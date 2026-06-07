import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid
from datetime import datetime, timezone

from app.firestore_client import get_firestore
from app.mail_accounts_config import get_account, smtp_uses_ssl


def _connect_smtp(account: dict):
    """
    Open an SMTP connection using the right handshake for the account's port:
      465 -> implicit TLS/SSL  (smtplib.SMTP_SSL)
      587 (or anything else) -> STARTTLS (smtplib.SMTP + .starttls())

    Different mailboxes on this project use different providers/hosts/ports
    (e.g. sales@blueboot.ai on cPanel:465 vs leif@blueboot.no on Gmail:587),
    so this MUST be resolved per-account, not assumed globally.
    """
    host, port = account["host"], account["port"]

    if smtp_uses_ssl(account):
        return smtplib.SMTP_SSL(host, port, timeout=30)

    server = smtplib.SMTP(host, port, timeout=30)
    server.ehlo()
    server.starttls()
    server.ehlo()
    return server


def _unsubscribe_settings():
    """
    Resolve UNSUBSCRIBE_BASE_URL / UNSUBSCRIBE_MAILTO straight from the
    environment. mail_accounts_config (imported above) already calls
    load_dotenv() against the project .env on import, so these are available
    here without re-reading any secret files. "smart-mail" has a hyphen and
    therefore isn't an importable package name -- this module purposefully
    does not depend on it. Lazy + catch-all -- never raises, never runs at
    import time, and a missing/blank value just disables the headers.
    """
    try:
        return (
            (os.getenv("UNSUBSCRIBE_BASE_URL") or "").strip(),
            (os.getenv("UNSUBSCRIBE_MAILTO") or "").strip(),
        )
    except Exception:
        return "", ""


def _build_unsubscribe_headers(contact_doc_id: str | None):
    """
    Build List-Unsubscribe (+ List-Unsubscribe-Post) header values.

    One-click unsubscribe is one of the strongest inbox-placement signals a
    bulk sender can offer -- mailbox providers (Gmail/Outlook/Yahoo) use its
    presence (and whether recipients use it vs. hitting "Report spam") as a
    deliverability/reputation signal. RFC 8058 defines the one-click POST
    variant via List-Unsubscribe-Post: List-Unsubscribe=One-Click.

    Returns (list_unsubscribe, list_unsubscribe_post) where either may be None
    if nothing is configured -- callers must treat both as optional.
    """
    base_url, mailto = _unsubscribe_settings()
    if not base_url and not mailto:
        return None, None

    targets = []
    if base_url:
        url = base_url.replace("{contact_id}", contact_doc_id or "")
        targets.append(f"<{url}>")
    if mailto:
        addr = mailto if mailto.lower().startswith("mailto:") else f"mailto:{mailto}"
        targets.append(f"<{addr}>")

    list_unsubscribe = ", ".join(targets)
    # RFC 8058 one-click POST is only valid when there's an HTTPS URL target.
    list_unsubscribe_post = "List-Unsubscribe=One-Click" if base_url else None
    return list_unsubscribe, list_unsubscribe_post


def send_email(
    to_email,
    subject,
    text_body,
    html_body=None,
    account: str | None = None,
    campaign_id: str | None = None,
    contact_doc_id: str | None = None,
):
    """Send an email via SMTP.

    Pass *account* (alias from mail_accounts_config.MAIL_ACCOUNTS, e.g. "sales"
    or "leif") to send from a specific mailbox; omit to use DEFAULT_MAIL_ACCOUNT.

    Account resolution happens here, lazily, on every call -- never at import
    time -- so a misconfigured account raises a clear error for THIS send only
    and can never crash the module (or anything that imports it) on load.
    """
    acc = get_account(account)

    username = acc["user"]
    password = acc["password"]
    from_hdr = f"{acc['from_name']} <{acc['from_addr']}>"

    msg = MIMEMultipart("alternative")

    msg["Subject"] = subject
    msg["From"] = from_hdr
    msg["To"] = to_email

    message_id = make_msgid()

    msg["Message-ID"] = message_id

    # One-click unsubscribe headers -- improves inbox placement (Gmail/Outlook/
    # Yahoo bulk-sender requirements). Added only when configured; never blocks
    # a send if UNSUBSCRIBE_BASE_URL / UNSUBSCRIBE_MAILTO are unset.
    list_unsubscribe, list_unsubscribe_post = _build_unsubscribe_headers(contact_doc_id)
    if list_unsubscribe:
        msg["List-Unsubscribe"] = list_unsubscribe
    if list_unsubscribe_post:
        msg["List-Unsubscribe-Post"] = list_unsubscribe_post

    # Plain text part
    text_part = MIMEText(text_body, "plain", "utf-8")
    msg.attach(text_part)

    # HTML part
    if html_body:
        html_part = MIMEText(html_body, "html", "utf-8")
        msg.attach(html_part)

    # Send email
    server = _connect_smtp(acc)
    try:
        server.login(username, password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass

    # Save to Firestore
    db = get_firestore()
    sent_at = datetime.now(timezone.utc).isoformat()

    db.collection("outreach_sent").add({
        "to_email": to_email,
        "subject": subject,
        "text_body": text_body,
        "html_body": html_body,

        "campaign_id": campaign_id,
        "sender_account": acc["alias"],
        "contact_doc_id": contact_doc_id,

        "list_unsubscribe": list_unsubscribe,

        "sent_at": sent_at,
        "message_id": message_id,
        "status": "sent"
    })

    print(f"Email sent to {to_email} via {acc['alias']} ({acc['host']}:{acc['port']})")

    return {
        "message_id": message_id,
        "sent_at": sent_at,
    }
