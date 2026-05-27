import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid
from datetime import datetime, timezone

from app.firestore_client import get_firestore
from mail_setup_secrets import get_account, SMTP

_account  = get_account()          # uses DEFAULT_ACCOUNT ("sales")
SMTP_HOST = SMTP["host"]
SMTP_PORT = SMTP["port"]

SMTP_USERNAME = _account["user"]
SMTP_PASSWORD = _account["password"]
SMTP_FROM     = f"{_account['from_name']} <{_account['from_addr']}>"

def send_email(to_email, subject, text_body, html_body=None, account: str | None = None):
    """Send an email via SMTP.

    Pass *account* (alias from mail_setup.MAIL_ACCOUNTS) to send from a
    specific address; omit to use the default account.
    """
    if account:
        acc      = get_account(account)
        username = acc["user"]
        password = acc["password"]
        from_hdr = f"{acc['from_name']} <{acc['from_addr']}>"
    else:
        username = SMTP_USERNAME
        password = SMTP_PASSWORD
        from_hdr = SMTP_FROM

    msg = MIMEMultipart("alternative")

    msg["Subject"] = subject
    msg["From"] = from_hdr
    msg["To"] = to_email

    message_id = make_msgid()

    msg["Message-ID"] = message_id

    # Plain text part
    text_part = MIMEText(text_body, "plain", "utf-8")
    msg.attach(text_part)

    # HTML part
    if html_body:
        html_part = MIMEText(html_body, "html", "utf-8")
        msg.attach(html_part)

    # Send email
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
        server.login(username, password)
        server.send_message(msg)

    # Save to Firestore
    db = get_firestore()

    db.collection("outreach_sent").add({
        "to_email": to_email,
        "subject": subject,
        "text_body": text_body,
        "html_body": html_body,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "message_id": message_id,
        "status": "sent"
    })

    print(f"Email sent to {to_email}")
