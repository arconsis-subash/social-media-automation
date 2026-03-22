import os
import re
import imaplib
import email
import smtplib
from dataclasses import dataclass
from pathlib import Path
from email.header import decode_header
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import APIStatusError, AuthenticationError, OpenAI, RateLimitError


# Load the project's .env before reading any module-level settings.
load_dotenv(dotenv_path=Path(__file__).with_name(".env"))


def get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name, default)
    if value is None:
        return None
    return value.strip()


def get_env_int(name: str, default: int) -> int:
    value = get_env(name)
    if not value:
        return default
    return int(value)


@dataclass(frozen=True)
class Settings:
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    gmail_address: Optional[str]
    gmail_app_password: Optional[str]
    email_to: Optional[str]
    openai_api_key: Optional[str]
    openai_model: str
    tldr_from_filter: str
    tldr_subject_filter: str
    max_emails: int
    max_candidates: int


SETTINGS = Settings(
    imap_host=get_env("GMAIL_IMAP_HOST", "imap.gmail.com") or "imap.gmail.com",
    imap_port=get_env_int("GMAIL_IMAP_PORT", 993),
    smtp_host=get_env("GMAIL_SMTP_HOST", "smtp.gmail.com") or "smtp.gmail.com",
    smtp_port=get_env_int("GMAIL_SMTP_PORT", 587),
    gmail_address=get_env("GMAIL_ADDRESS"),
    gmail_app_password=get_env("GMAIL_APP_PASSWORD"),
    email_to=get_env("EMAIL_TO") or get_env("GMAIL_ADDRESS"),
    openai_api_key=get_env("OPENAI_API_KEY"),
    openai_model=get_env("OPENAI_MODEL", "gpt-4.1-mini") or "gpt-4.1-mini",
    tldr_from_filter=get_env("TLDR_FROM_FILTER", "tldr") or "tldr",
    tldr_subject_filter=get_env("TLDR_SUBJECT_FILTER", "TLDR") or "TLDR",
    max_emails=get_env_int("MAX_EMAILS", 5),
    max_candidates=get_env_int("MAX_CANDIDATES", 12),
)

OPENAI_CLIENT = OpenAI(api_key=SETTINGS.openai_api_key) if SETTINGS.openai_api_key else None


class OpenAIQuotaError(RuntimeError):
    pass


def create_openai_response(prompt: str) -> str:
    if OPENAI_CLIENT is None:
        raise RuntimeError("OPENAI_API_KEY is missing or invalid.")

    try:
        response = OPENAI_CLIENT.responses.create(
            model=SETTINGS.openai_model,
            input=prompt,
        )
    except AuthenticationError as exc:
        raise RuntimeError(
            "OpenAI authentication failed. Check OPENAI_API_KEY in your .env file."
        ) from exc
    except RateLimitError as exc:
        raise OpenAIQuotaError(
            "OpenAI request failed because your API project has no available quota or billing "
            "is not set up. Add credits or enable billing in the OpenAI dashboard, then try again."
        ) from exc
    except APIStatusError as exc:
        raise RuntimeError(
            f"OpenAI API request failed with status {exc.status_code}. Please try again shortly."
        ) from exc

    return response.output_text.strip()


def fallback_pick_topic(candidates: List[Dict[str, str]]) -> Dict[str, str]:
    chosen = candidates[0].copy()
    chosen["why_this_wins"] = (
        "Picked as a fallback because OpenAI quota is unavailable. "
        "It was the first recent TLDR headline that passed the filters."
    )
    return chosen


def fallback_script(topic: Dict[str, str]) -> str:
    return f"""
HOOK:
This tech story is too good to ignore.

SCRIPT:
Here is a fast update pulled from today’s TLDR-style newsletter.
The headline is: {topic['headline']}
If you want to turn this into a short video, start by explaining why it matters,
what makes it surprising, and who in tech should care.
Then point viewers to the source link for the full story.

ON_SCREEN_TEXT:
1. Tech headline of the day
2. {topic['headline']}
3. Why this matters
4. Who should care
5. Read more at the source link

CAPTION:
Quick tech update from today’s newsletter roundup.

HASHTAGS:
#tech #ai #developers #softwareengineering #startups #programming #news #automation
""".strip()


def require_env() -> None:
    required = {
        "GMAIL_ADDRESS": SETTINGS.gmail_address,
        "GMAIL_APP_PASSWORD": SETTINGS.gmail_app_password,
        "OPENAI_API_KEY": SETTINGS.openai_api_key,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")

    app_password = SETTINGS.gmail_app_password or ""
    normalized_password = re.sub(r"\s+", "", app_password)
    if len(normalized_password) != 16 or not normalized_password.isalpha():
        raise RuntimeError(
            "GMAIL_APP_PASSWORD looks invalid. Use a Gmail App Password from your Google account, "
            "not your normal Gmail password. It should be 16 letters."
        )


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
    mail = imaplib.IMAP4_SSL(SETTINGS.imap_host, SETTINGS.imap_port)
    password = re.sub(r"\s+", "", SETTINGS.gmail_app_password or "")
    try:
        mail.login(SETTINGS.gmail_address, password)
    except imaplib.IMAP4.error as exc:
        raise RuntimeError(
            "Gmail IMAP authentication failed. Confirm IMAP is enabled for the account and "
            "GMAIL_APP_PASSWORD is a valid 16-letter Google App Password for "
            f"{SETTINGS.gmail_address}."
        ) from exc
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
    return (
        SETTINGS.tldr_from_filter.lower() in sender_l
        or SETTINGS.tldr_subject_filter.lower() in subject_l
    )


def fetch_recent_tldr_candidates() -> List[Dict[str, str]]:
    candidates: List[Dict[str, str]] = []
    seen_headlines = set()
    matched_count = 0
    bad_patterns = {
        "unsubscribe", "advertise", "sponsor", "read online", "view in browser",
        "jobs", "podcast", "instagram", "linkedin", "twitter", "x.com",
        "privacy", "terms", "feedback",
    }

    mail = gmail_login()
    try:
        email_ids = search_tldr_emails(mail)

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

            for item in links:
                title = item["title"]
                url = item["url"]
                normalized = title.lower().strip()

                if normalized in seen_headlines:
                    continue
                if any(pattern in normalized for pattern in bad_patterns):
                    continue
                if len(title) < 12 or len(title) > 180:
                    continue

                seen_headlines.add(normalized)
                candidates.append({
                    "email_subject": subject,
                    "sender": sender,
                    "date": dt,
                    "headline": title,
                    "url": url,
                    "context": body_text[:4000],
                })

                if len(candidates) >= SETTINGS.max_candidates:
                    break

            if matched_count >= SETTINGS.max_emails or len(candidates) >= SETTINGS.max_candidates:
                break
    finally:
        try:
            mail.logout()
        except Exception:
            pass

    return candidates[:SETTINGS.max_candidates]


def pick_best_topic(candidates: List[Dict[str, str]]) -> Dict[str, str]:
    if not candidates:
        raise RuntimeError("No TLDR candidates found in Gmail.")

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

    text = create_openai_response(prompt)

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

    return create_openai_response(prompt)


def send_email(subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = SETTINGS.gmail_address
    msg["To"] = SETTINGS.email_to
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(SETTINGS.smtp_host, SETTINGS.smtp_port) as server:
        server.starttls()
        try:
            server.login(
                SETTINGS.gmail_address,
                re.sub(r"\s+", "", SETTINGS.gmail_app_password or ""),
            )
        except smtplib.SMTPAuthenticationError as exc:
            raise RuntimeError(
                "Gmail SMTP authentication failed. Use the same 16-letter Google App Password "
                "that works for IMAP."
            ) from exc
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

    used_fallback = False

    try:
        print("Selecting best topic...")
        topic = pick_best_topic(candidates)

        print(f"Chosen topic: {topic['headline']}")
        print("Generating script...")
        script = generate_script(topic)
    except OpenAIQuotaError as exc:
        used_fallback = True
        print(f"Warning: {exc}")
        print("Falling back to a non-AI summary...")
        topic = fallback_pick_topic(candidates)
        script = fallback_script(topic)

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

    if used_fallback:
        final_email = (
            "OpenAI quota was unavailable, so this email was generated with the fallback mode.\n\n"
            f"{final_email}"
        )

    print("Sending result email...")
    send_email(
        subject=f"Generated Reel Script: {topic['headline'][:120]}",
        body=final_email,
    )

    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"Error: {exc}")
