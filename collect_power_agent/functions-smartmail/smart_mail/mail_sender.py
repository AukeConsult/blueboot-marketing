# functions-smartmail/smart_mail/mail_sender.py
"""
Adapted copy of app/mail_sender.py for the deployed Cloud Function codebase.
Logic is byte-identical; only the two Firestore/account imports are relative
(`.firestore_client` / `.mail_accounts_config`) so the module resolves inside
the `smart_mail` package instead of the local `app` package.
"""
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid
from datetime import datetime, timezone

from .mail_accounts_config import get_account, smtp_uses_ssl


def _connect_smtp(account: dict):
    """
    Open an SMTP connection using the right handshake for the account's port:
      465 -> implicit TLS/SSL  (smtplib.SMTP_SSL)
      587 (or anything else) -> STARTTLS (smtplib.SMTP + .starttls())
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
    Resolve UNSUBSCRIBE_BASE_URL / UNSUBSCRIBE_MAILTO from the environment.
    In the deployed function these come from .env.<project-id> (loaded
    natively by the Cloud Functions runtime) -- never re-read any secret
    files. Lazy + catch-all -- never raises, never runs at import time.
    """
    try:
        return (
            (os.getenv("UNSUBSCRIBE_BASE_URL") or "").strip(),
            (os.getenv("UNSUBSCRIBE_MAILTO") or "").strip(),
        )
    except Exception:
        return "", ""


def _build_unsubscribe_headers(contact_doc_id: str | None):
    """Build (List-Unsubscribe, List-Unsubscribe-Post) header values; either may be None."""
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
    """Send an email via SMTP. Outreach writeback is handled by confirm_sent()."""
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

    list_unsubscribe, list_unsubscribe_post = _build_unsubscribe_headers(contact_doc_id)
    if list_unsubscribe:
        msg["List-Unsubscribe"] = list_unsubscribe
    if list_unsubscribe_post:
        msg["List-Unsubscribe-Post"] = list_unsubscribe_post

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    if html_body:
        msg.attach(MIMEText(html_body, "html", "utf-8"))

    server = _connect_smtp(acc)
    try:
        server.login(username, password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass

    sent_at = datetime.now(timezone.utc).isoformat()

    print(f"Email sent to {to_email} via {acc['alias']} ({acc['host']}:{acc['port']})")

    return {"message_id": message_id, "sent_at": sent_at}
