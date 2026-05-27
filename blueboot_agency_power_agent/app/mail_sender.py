import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid
from datetime import datetime, timezone

from app.firestore_client import get_firestore
from blueboot_secrets import smtpConfig

SMTP_HOST = smtpConfig["host"]
SMTP_PORT = smtpConfig["port"]

SMTP_USERNAME = smtpConfig["user"]
SMTP_PASSWORD = smtpConfig["pass"]

SMTP_FROM = smtpConfig["from"]


def send_email(to_email, subject, text_body, html_body=None):
    msg = MIMEMultipart("alternative")

    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
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
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
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
