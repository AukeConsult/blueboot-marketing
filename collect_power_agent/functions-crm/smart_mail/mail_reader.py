# functions-crm/smart_mail/mail_reader.py
"""
Adapted copy of app/mail_reader.py. IMAP credentials are loaded from
Firestore (settings/mail_accounts/accounts/{email}) — same source as the
sender — so no separate env-var password or Secret Manager secret is needed.

`account` passed to read_unread_emails() must be the full email address
stored in Firestore, e.g. 'sales@blueboot.ai'. Set REPLY_ACCOUNTS to a
comma-separated list of such addresses in .env.<project-id>.
"""
import imaplib
import email
from email.header import decode_header
from datetime import datetime, timezone

from .firestore_client import get_firestore


def decode_mime(value):
    if not value:
        return ""
    decoded_parts = decode_header(value)
    result = []
    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            result.append(part.decode(encoding or "utf-8", errors="ignore"))
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


def _load_imap_account(db, account_email: str) -> dict:
    """Load IMAP credentials from Firestore settings/mail_accounts/accounts/{email}."""
    key = (account_email or "").strip().lower()
    if not key:
        raise RuntimeError("[mail_reader] No account email provided")
    doc = (
        db.collection("settings")
        .document("mail_accounts")
        .collection("accounts")
        .document(key)
        .get()
    )
    if not doc.exists:
        raise RuntimeError(
            f"[mail_reader] No Firestore mail account found for '{key}' "
            f"-- add a document at settings/mail_accounts/accounts/{key}"
        )
    return doc.to_dict() or {}


def read_unread_emails(account: str | None = None):
    """Poll one mailbox INBOX for unread messages; store in `inbox_messages` (deduped by Message-ID).

    `account` must be the full email address stored in Firestore
    (e.g. 'sales@blueboot.ai'). Credentials — host, port, username,
    password — are loaded from settings/mail_accounts/accounts/{account}.
    """
    db = get_firestore()
    d = _load_imap_account(db, account or "")

    imap_host = str(d.get("imap_host") or d.get("host") or "").strip()
    imap_port = int(d.get("port") or d.get("imap_port") or 993)
    user      = str(d.get("username") or d.get("email") or account or "").strip()
    password  = d.get("password", "")
    alias     = str(d.get("email") or account or "").strip()

    if not imap_host:
        raise RuntimeError(
            f"[mail_reader] No IMAP host in Firestore account '{alias}' "
            f"-- set 'imap_host' or 'host' on the account document"
        )

    mail = imaplib.IMAP4_SSL(imap_host, imap_port)

    try:
        mail.login(user, password)
        mail.select("INBOX")
        status, messages = mail.search(None, "UNSEEN")
        email_ids = messages[0].split()
        print(f"[{alias}] Found {len(email_ids)} unread emails")

        for email_id in email_ids:
            status, msg_data = mail.fetch(email_id, "(RFC822)")
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)
            subject    = decode_mime(msg.get("Subject"))
            from_email = decode_mime(msg.get("From"))

            message_id  = msg.get("Message-ID")
            in_reply_to = msg.get("In-Reply-To")
            references  = msg.get("References")

            body_text = extract_text(msg)

            existing = (
                db.collection("inbox_messages")
                .where("message_id", "==", message_id)
                .limit(1)
                .stream()
            )

            if any(existing):
                print(f"Skipping duplicate: {message_id}")
                continue

            db.collection("inbox_messages").add({
                "account":       alias,
                "from_email":    from_email,
                "subject":       subject,
                "body_text":     body_text,
                "received_at":   datetime.now(timezone.utc).isoformat(),
                "message_id":    message_id,
                "in_reply_to":   in_reply_to,
                "references":    references,
                "imap_uid":      email_id.decode(),
                "processed_at":  datetime.now(timezone.utc).isoformat(),
                "reply_matched": False,
            })

            print(f"Saved email: {subject}")
    finally:
        try:
            mail.logout()
        except Exception:
            pass
