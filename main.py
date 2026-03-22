import os
import re
import imaplib
import email
import smtplib
from email.header import decode_header
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
from openai import OpenAI


IMAP_HOST = os.getenv("GMAIL_IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.getenv("GMAIL_IMAP_PORT", "993"))
SMTP_HOST = os.getenv("GMAIL_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("GMAIL_SMTP_PORT", "587"))

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
EMAIL_TO = os.getenv("EMAIL_TO", GMAIL_ADDRESS)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

TLDR_FROM_FILTER = os.getenv("TLDR_FROM_FILTER", "tldr")
TLDR_SUBJECT_FILTER = os.getenv("TLDR_SUBJECT_FILTER", "TLDR")
MAX_EMAILS = int(os.getenv("MAX_EMAILS", "5"))
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", "12"))


def require_env() -> None:
    required = {
        "GMAIL_ADDRESS": GMAIL_ADDRESS,
        "GMAIL_APP_PASSWORD": GMAIL_APP_PASSWORD,
        "OPENAI_API_KEY": OPENAI_API_KEY,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")


def decode_mime_words(text: Optional[str]) -> str:
    if not text:
        return ""
    parts = decode_header(text)
    decoded = []
    for part, enc in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def clean_text(text: str) -> str:
    text = re.sub(r"\r", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def gmail_login() -> imaplib.IMAP4_SSL:
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
    return mail


def search_tldr_emails(mail: imaplib.IMAP4_SSL) -> List[bytes]:
    mail.select("INBOX")

    # Broad search first; filtering happens in Python.
    status, data = mail.search(None, "ALL")
    if status != "OK":
        raise RuntimeError("Failed to search mailbox.")

    email_ids = data[0].split()
    email_ids.reverse()
    return email_ids[:50]


def get_email_body(msg: email.message.Message) -> str:
    html_body = None
    text_body = None

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in content_disposition.lower():
                continue

            payload = part.get_payload(decode=True)
            if payload is None:
                continue

            charset = part.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except LookupError:
                decoded = payload.decode("utf-8", errors="replace")

            if content_type == "text/html" and html_body is None:
                html_body = decoded
            elif content_type == "text/plain" and text_body is None:
                text_body = decoded
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except LookupError:
                decoded = payload.decode("utf-8", errors="replace")

            if msg.get_content_type() == "text/html":
                html_body = decoded
            else:
                text_body = decoded

    if html_body:
        soup = BeautifulSoup(html_body, "html.parser")
        return clean_text(soup.get_text("\n"))

    return clean_text(text_body or "")


def extract_links_from_html(msg: email.message.Message) -> List[Dict[str, str]]:
    links = []
    html_body = None

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        html_body = payload.decode(charset, errors="replace")
                    except LookupError:
                        html_body = payload.decode("utf-8", errors="replace")
                    break
    else:
        if msg.get_content_type() == "text/html":
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                try:
                    html_body = payload.decode(charset, errors="replace")
                except LookupError:
                    html_body = payload.decode("utf-8", errors="replace")

    if not html_body:
        return links

    soup = BeautifulSoup(html_body, "html.parser")
    for a in soup.find_all("a", href=True):
        text = clean_text(a.get_text(" ", strip=True))
        href = a["href"].strip()
        if not text or not href.startswith("http"):
            continue
        if len(text) < 8:
            continue
        links.append({"title": text, "url": href})

    return links


def looks_like_tldr_newsletter(sender: str, subject: str) -> bool:
    sender_l = sender.lower()
    subject_l = subject.lower()
    return TLDR_FROM_FILTER.lower() in sender_l or TLDR_SUBJECT_FILTER.lower() in subject_l


def fetch_recent_tldr_candidates() -> List[Dict[str, str]]:
    mail = gmail_login()
    email_ids = search_tldr_emails(mail)

    candidates: List[Dict[str, str]] = []
    matched_count = 0

    for email_id in email_ids:
        status, msg_data = mail.fetch(email_id, "(RFC822)")
        if status != "OK":
            continue

        raw_msg = msg_data[0][1]
        msg = email.message_from_bytes(raw_msg)

        subject = decode_mime_words(msg.get("Subject", ""))
        sender = decode_mime_words(msg.get("From", ""))
        date_raw = msg.get("Date", "")

        if not looks_like_tldr_newsletter(sender, subject):
            continue

        matched_count += 1
        body_text = get_email_body(msg)
        links = extract_links_from_html(msg)

        try:
            dt = parsedate_to_datetime(date_raw).isoformat()
        except Exception:
            dt = date_raw

        # Try to collect interesting headline-like links from newsletter
        seen = set()
        for item in links:
            title = item["title"]
            url = item["url"]

            normalized = title.lower().strip()
            if normalized in seen:
                continue
            seen.add(normalized)

            # Filter obvious footer/nav links
            bad_patterns = [
                "unsubscribe", "advertise", "sponsor", "read online", "view in browser",
                "jobs", "podcast", "instagram", "linkedin", "twitter", "x.com",
                "privacy", "terms", "feedback"
            ]
            if any(bp in normalized for bp in bad_patterns):
                continue

            if len(title) < 12 or len(title) > 180:
                continue

            candidates.append({
                "email_subject": subject,
                "sender": sender,
                "date": dt,
                "headline": title,
                "url": url,
                "context": body_text[:4000],
            })

            if len(candidates) >= MAX_CANDIDATES:
                break

        if matched_count >= MAX_EMAILS or len(candidates) >= MAX_CANDIDATES:
            break

    mail.logout()
    return candidates[:MAX_CANDIDATES]


def pick_best_topic(candidates: List[Dict[str, str]]) -> Dict[str, str]:
    if not candidates:
        raise RuntimeError("No TLDR candidates found in Gmail.")

    client = OpenAI(api_key=OPENAI_API_KEY)

    formatted = []
    for idx, c in enumerate(candidates, start=1):
        formatted.append(
            f"{idx}. Headline: {c['headline']}\n"
            f"   URL: {c['url']}\n"
            f"   Newsletter: {c['email_subject']}\n"
            f"   Date: {c['date']}\n"
        )

    prompt = f"""
You are picking ONE topic for a short-form tech reel.

Choose the most interesting topic based on:
- surprising or weird
- high meme potential
- easy to explain in under 45 seconds
- relevant to developers / AI / startups / software / internet culture
- likely to make people stop scrolling

Return STRICT JSON with keys:
headline
url
why_this_wins
index

Candidates:
{chr(10).join(formatted)}
""".strip()

    response = client.responses.create(
        model=OPENAI_MODEL,
        input=prompt,
    )

    text = response.output_text.strip()

    # Very lightweight JSON extraction
    import json
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise RuntimeError(f"Could not parse topic selection response:\n{text}")

    parsed = json.loads(match.group(0))
    idx = int(parsed["index"]) - 1
    if idx < 0 or idx >= len(candidates):
        raise RuntimeError("Model returned invalid candidate index.")

    chosen = candidates[idx].copy()
    chosen["why_this_wins"] = parsed.get("why_this_wins", "")
    return chosen


def generate_script(topic: Dict[str, str]) -> str:
    client = OpenAI(api_key=OPENAI_API_KEY)

    prompt = f"""
You are writing a funny, smart, fast-paced Instagram Reel / YouTube Shorts script.

Audience:
- developers
- AI-curious people
- tech viewers
- startup / software people

Style:
- witty
- slightly sarcastic
- not cringe
- not robotic
- factual but entertaining
- strong hook in first 2 seconds

Topic:
Headline: {topic['headline']}
URL: {topic['url']}
Why chosen: {topic.get('why_this_wins', '')}

Write:
1. A scroll-stopping HOOK
2. A 30-45 second voiceover script
3. On-screen text suggestions for each beat
4. A short caption
5. 8 hashtags

Format exactly like this:

HOOK:
...

SCRIPT:
...

ON_SCREEN_TEXT:
1. ...
2. ...
3. ...
4. ...
5. ...

CAPTION:
...

HASHTAGS:
...
""".strip()

    response = client.responses.create(
        model=OPENAI_MODEL,
        input=prompt,
    )

    return response.output_text.strip()


def send_email(subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.send_message(msg)


def main() -> None:
    require_env()

    print("Fetching TLDR newsletter topics from Gmail...")
    candidates = fetch_recent_tldr_candidates()

    if not candidates:
        raise RuntimeError(
            "No TLDR-like newsletter emails found. Check sender/subject filters or inbox labels."
        )

    print(f"Found {len(candidates)} candidate links.")

    print("Selecting best topic...")
    topic = pick_best_topic(candidates)

    print(f"Chosen topic: {topic['headline']}")
    print("Generating script...")
    script = generate_script(topic)

    final_email = f"""
Chosen Topic:
{topic['headline']}

URL:
{topic['url']}

Why This Won:
{topic.get('why_this_wins', '')}

========================
GENERATED REEL SCRIPT
========================

{script}
""".strip()

    print("Sending result email...")
    send_email(
        subject=f"Generated Reel Script: {topic['headline'][:120]}",
        body=final_email,
    )

    print("Done.")


if __name__ == "__main__":
    main()