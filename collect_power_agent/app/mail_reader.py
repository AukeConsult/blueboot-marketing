import imaplib
import email
from email.header import decode_header
from datetime import datetime, timezone

from firestore_client import get_firestore
from mail_accounts_config import get_account


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


def read_unread_emails(account: str | None = None):
    """
    Poll one mailbox's INBOX for unread messages and store them in
    `inbox_messages` (deduped by Message-ID).

    Account resolution happens here, lazily, on every call -- never at import
    time -- so a misconfigured/missing mailbox raises a clear error for THIS
    poll only and can never crash the module (or anything importing it) on load.
    """
    acc = get_account(account)

    imap_host = acc["imap_host"]
    imap_port = acc["imap_port"]

    if not imap_host:
        raise RuntimeError(
            f"[mail_reader] No IMAP host configured for account '{acc['alias']}' "
            f"-- set {acc['alias'].upper()}_IMAP_HOST / _IMAP_PORT in .env"
        )

    db = get_firestore()
    mail = imaplib.IMAP4_SSL(imap_host, imap_port)

    try:
        mail.login(acc["user"], acc["password"])
        mail.select("INBOX")
        status, messages = mail.search(None, "UNSEEN")
        email_ids = messages[0].split()
        print(f"[{acc['alias']}] Found {len(email_ids)} unread emails")

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
                "account": acc["alias"],
                "from_email": from_email,
                "subject": subject,
                "body_text": body_text,
                "received_at": datetime.now(timezone.utc).isoformat(),
                "message_id": message_id,
                "in_reply_to": in_reply_to,
                "references": references,
                "imap_uid": email_id.decode(),
                "processed_at": datetime.now(timezone.utc).isoformat(),
                # Reply-matching pipeline (smart_reply_matcher.py) queries on
                # this flag to find work; it flips to True once a match attempt
                # has been made (whether or not it succeeded) so messages are
                # never reprocessed forever.
                "reply_matched": False,
            })

            print(f"Saved email: {subject}")
    finally:
        try:
            mail.logout()
        except Exception:
            pass


if __name__ == "__main__":
    read_unread_emails()
