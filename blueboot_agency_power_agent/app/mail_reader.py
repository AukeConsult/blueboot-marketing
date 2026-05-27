import imaplib
import email
from email.header import decode_header
from datetime import datetime, timezone

from app.firestore_client import get_firestore
from mail_setup_secrets import IMAP, get_account

_account      = get_account()          # uses DEFAULT_ACCOUNT ("sales")
IMAP_HOST     = IMAP["host"]
IMAP_PORT     = IMAP["port"]
IMAP_USERNAME = _account["user"]
IMAP_PASSWORD = _account["password"]

def decode_mime(value):
    if not value:
        return ""

    decoded_parts = decode_header(value)
    result = []

    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            result.append(
                part.decode(encoding or "utf-8", errors="ignore")
            )
        else:
            result.append(part)

    return "".join(result)


def extract_text(msg):
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition"))

            if content_type == "text/plain" and "attachment" not in disposition:
                payload = part.get_payload(decode=True)

                if payload:
                    return payload.decode(errors="ignore")

    else:
        payload = msg.get_payload(decode=True)

        if payload:
            return payload.decode(errors="ignore")

    return ""


def read_unread_emails():
    db = get_firestore()
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(IMAP_USERNAME, IMAP_PASSWORD)
    mail.select("INBOX")
    status, messages = mail.search(None, "UNSEEN")
    email_ids = messages[0].split()
    print(f"Found {len(email_ids)} unread emails")

    for email_id in email_ids:
        status, msg_data = mail.fetch(email_id, "(RFC822)")
        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)
        subject = decode_mime(msg.get("Subject"))
        from_email = decode_mime(msg.get("From"))

        message_id = msg.get("Message-ID")
        in_reply_to = msg.get("In-Reply-To")
        references = msg.get("References")

        body_text = extract_text(msg)

        # Duplicate prevention
        existing = db.collection("inbox_messages") \
            .where("message_id", "==", message_id) \
            .limit(1) \
            .stream()

        if any(existing):
            print(f"Skipping duplicate: {message_id}")
            continue

        db.collection("inbox_messages").add({
            "from_email": from_email,
            "subject": subject,
            "body_text": body_text,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "message_id": message_id,
            "in_reply_to": in_reply_to,
            "references": references,
            "imap_uid": email_id.decode(),
            "processed_at": datetime.now(timezone.utc).isoformat()
        })

        print(f"Saved email: {subject}")

    mail.logout()


if __name__ == "__main__":
    read_unread_emails()