"""followup_email_sync_lib.py -- Sync email history into campaign contact follow-up logs.

For each outreach mail account, fetches recent emails (inbox + sent) and matches
them against campaign_contacts by email address. Matched emails are appended to
the contact's comment_history array using Firestore ArrayUnion — idempotent because
each entry carries a unique email_id; identical maps are never inserted twice.

Used by the crmWorker 'followup-email-sync' job.

Parameters (all optional):
  campaign_id     str   Only sync contacts belonging to this campaign
  contact_doc_id  str   Only sync this specific contact (requires campaign_id)
  days            int   Lookback window in days (default 7, 0 = all time)
"""
from __future__ import annotations

import imaplib
import re
import ssl
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.header import decode_header
from email.utils import parsedate_to_datetime

CAMPAIGNS_COLLECTION   = "campaigns"
CONTACTS_SUBCOLLECTION = "campaign_contacts"
SETTINGS_COLLECTION    = "settings"
MAIL_ACCOUNTS_DOC      = "mail_accounts"

_SKIP_FOLDERS = {
    "trash", "spam", "junk", "deleted items", "deleted messages",
    "[gmail]/trash", "[gmail]/spam", "[gmail]/important",
    "[gmail]/all mail", "[gmail]/starred",
}
_SENT_CANDIDATES = [
    "Sent", "Sent Items", "Sent Messages",
    "[Gmail]/Sent Mail", "INBOX.Sent",
]


# ── IMAP / address helpers ────────────────────────────────────────────────────

def _decode_str(val: str | None) -> str:
    if not val:
        return ""
    parts = decode_header(val)
    out = []
    for raw, enc in parts:
        if isinstance(raw, bytes):
            out.append(raw.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(raw)
    return " ".join(out)


def _extract_email(addr: str) -> str:
    """Return bare lowercase email from 'Name <email@x.com>' or plain address."""
    if not addr:
        return ""
    m = re.search(r"<([^>]+)>", addr)
    return (m.group(1) if m else addr).strip().lower()


def _msg_key(message_id: str, folder: str, uid: str) -> str:
    """Stable dedup key — matches frontend emailMsgKey()."""
    if message_id:
        return re.sub(r"[/]", "_", message_id.strip().lstrip("<").rstrip(">"))[:500]
    return re.sub(r"[/]", "_", f"{folder}__{uid}")


def _find_sent_folder(conn: imaplib.IMAP4) -> str | None:
    for name in _SENT_CANDIDATES:
        try:
            quoted = '"' + name.replace('"', '\\"') + '"'
            typ, _ = conn.select(quoted, readonly=True)
            if typ == "OK":
                conn.close()
                return name
        except Exception:
            pass
    return None


def _imap_connect(ma: dict, account_email: str) -> imaplib.IMAP4:
    account_type = ma.get("account_type", "imap")

    if account_type == "imap":
        host    = ma.get("host", "").strip()
        port    = int(ma.get("port") or 993)
        use_ssl = ma.get("ssl", True)
        if not host:
            raise ValueError(f"IMAP host not configured for {account_email}")
        ctx  = ssl.create_default_context()
        conn = imaplib.IMAP4_SSL(host, port, ssl_context=ctx) if use_ssl else imaplib.IMAP4(host, port)
        conn.login(ma.get("username", ""), ma.get("password", ""))
        return conn

    if account_type == "gmail":
        import json, urllib.parse, urllib.request
        access_token  = ma.get("access_token",  "").strip()
        refresh_token = ma.get("refresh_token", "").strip()
        if not access_token:
            p = (
                f"client_id={urllib.parse.quote(ma.get('client_id',''))}"
                f"&client_secret={urllib.parse.quote(ma.get('client_secret',''))}"
                f"&refresh_token={urllib.parse.quote(refresh_token)}"
                "&grant_type=refresh_token"
            ).encode()
            req = urllib.request.Request(
                "https://oauth2.googleapis.com/token", data=p,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                td = json.loads(resp.read())
            access_token = td.get("access_token", "")
            if not access_token:
                raise ValueError(f"Gmail token refresh failed: {td.get('error_description', 'unknown')}")
        auth_str = f"user={account_email}auth=Bearer {access_token}"
        conn = imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=ssl.create_default_context())
        conn.authenticate("XOAUTH2", lambda _: auth_str.encode())
        return conn

    raise ValueError(f"Unsupported account_type '{account_type}'")


def _fetch_headers(conn: imaplib.IMAP4, folder: str, cutoff: datetime | None, limit: int) -> list[dict]:
    """Fetch message headers from a folder, filtered by SINCE date if given."""
    if folder.lower() in _SKIP_FOLDERS:
        return []
    try:
        quoted = '"' + folder.replace('"', '\\"') + '"'
        typ, _ = conn.select(quoted, readonly=True)
        if typ != "OK":
            typ, _ = conn.select(folder, readonly=True)
        if typ != "OK":
            return []

        criteria = f"SINCE {cutoff.strftime('%d-%b-%Y')}" if cutoff else "ALL"
        typ, data = conn.uid("search", None, criteria)
        if typ != "OK" or not data[0]:
            return []
        all_uids = data[0].split()
        if not all_uids:
            return []

        batch   = all_uids[-limit:]
        uid_set = b",".join(batch)
        typ, raw = conn.uid(
            "fetch", uid_set,
            "(UID BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])"
        )
        if typ != "OK" or not raw:
            return []

        msgs = []
        for item in raw:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            meta = item[0] if isinstance(item[0], bytes) else b""
            hdr  = item[1] if isinstance(item[1], bytes) else b""
            uid_m = re.search(rb"UID\s+(\d+)", meta)
            uid   = uid_m.group(1).decode() if uid_m else ""
            parsed = message_from_bytes(hdr)
            mid    = parsed.get("Message-ID", "").strip()
            subj   = _decode_str(parsed.get("Subject", "")) or "(no subject)"
            from_  = _decode_str(parsed.get("From", ""))
            to_    = _decode_str(parsed.get("To", ""))
            raw_d  = parsed.get("Date", "")
            try:
                date_str = parsedate_to_datetime(raw_d).isoformat()
            except Exception:
                date_str = datetime.now(timezone.utc).isoformat()
            msgs.append({
                "uid": uid, "message_id": mid, "folder": folder,
                "subject": subj, "from": from_, "to": to_, "date": date_str,
            })
        return msgs
    except Exception as exc:
        print(f"[followup-email-sync] folder '{folder}' error: {exc}", flush=True)
        return []


# ── Main entry point ──────────────────────────────────────────────────────────

def run_followup_email_sync(
    db,
    campaign_id:      str | None = None,
    contact_doc_id:   str | None = None,
    days:             int = 7,
    outreach_account: str | None = None,
) -> dict:
    """Fetch email history for campaign contacts and write to comment_history.

    Each entry is appended using Firestore ArrayUnion, so the operation is
    idempotent — identical email_id values are never inserted twice.
    """
    from google.cloud import firestore

    now    = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)) if days > 0 else None
    limit  = 500   # messages per folder

    # ── 1. Load all mail accounts ─────────────────────────────────────────────
    ma_col  = (db.collection(SETTINGS_COLLECTION)
                 .document(MAIL_ACCOUNTS_DOC)
                 .collection("accounts"))
    all_mas = {d.id: d.to_dict() for d in ma_col.stream()}
    if not all_mas:
        return {"error": "No mail accounts configured", "synced_entries": 0, "synced_contacts": 0}

    # ── 2. Build: account_email → [(campaign_id, doc_id, contact_email, ref)] ─
    camps_col = db.collection(CAMPAIGNS_COLLECTION)
    account_contacts: dict[str, list] = {acc: [] for acc in all_mas}

    if campaign_id and contact_doc_id:
        ref  = (camps_col.document(campaign_id)
                         .collection(CONTACTS_SUBCOLLECTION)
                         .document(contact_doc_id))
        snap = ref.get()
        if snap.exists:
            c    = snap.to_dict() or {}
            cd   = camps_col.document(campaign_id).get().to_dict() or {}
            acc  = (cd.get("outreach_email_account") or "").strip().lower()
            if acc in account_contacts:
                account_contacts[acc].append((campaign_id, contact_doc_id, c.get("email", ""), ref))

    elif campaign_id:
        cd  = camps_col.document(campaign_id).get().to_dict() or {}
        acc = (cd.get("outreach_email_account") or "").strip().lower()
        if acc in account_contacts:
            for doc in camps_col.document(campaign_id).collection(CONTACTS_SUBCOLLECTION).stream():
                c = doc.to_dict() or {}
                account_contacts[acc].append((campaign_id, doc.id, c.get("email", ""), doc.reference))

    else:
        for camp_doc in camps_col.stream():
            cid = camp_doc.id
            cd  = camp_doc.to_dict() or {}
            acc = (cd.get("outreach_email_account") or "").strip().lower()
            if not acc or acc not in account_contacts:
                continue
            for doc in camps_col.document(cid).collection(CONTACTS_SUBCOLLECTION).stream():
                c = doc.to_dict() or {}
                account_contacts[acc].append((cid, doc.id, c.get("email", ""), doc.reference))

    # ── 2b. Filter to selected outreach account ──────────────────────────────
    if outreach_account:
        acc_key = outreach_account.strip().lower()
        account_contacts = {k: v for k, v in account_contacts.items() if k == acc_key}

    # ── 3. Per account: connect, fetch, match, write ──────────────────────────
    total_entries  = 0
    total_contacts = 0
    errors: list[str] = []

    for acc_email, contacts in account_contacts.items():
        if not contacts:
            continue
        ma = all_mas[acc_email]

        # Build fast lookup: bare_email → ref
        contact_index: dict[str, object] = {}
        for _cid, _did, cemail, ref in contacts:
            if cemail:
                contact_index[cemail.lower()] = ref

        if not contact_index:
            continue

        print(f"[followup-email-sync] {acc_email} — {len(contact_index)} contacts", flush=True)

        try:
            conn = _imap_connect(ma, acc_email)
        except Exception as exc:
            errors.append(f"{acc_email}: connect failed — {exc}")
            continue

        try:
            sent_folder = _find_sent_folder(conn)
            folders_dirs = [("INBOX", False)] + ([(sent_folder, True)] if sent_folder else [])

            # contact_email → [entry, ...]
            contact_entries: dict[str, list] = {}

            for folder, is_sent in folders_dirs:
                for msg in _fetch_headers(conn, folder, cutoff, limit):
                    from_addr = _extract_email(msg["from"])
                    to_addrs  = [_extract_email(a) for a in re.split(r"[,;]", msg["to"])]
                    match_e   = to_addrs[0] if is_sent else from_addr
                    if not match_e or match_e not in contact_index:
                        continue
                    entry = {
                        "email_id": _msg_key(msg["message_id"], folder, msg["uid"]),
                        "type":     "EMAIL_OUT" if is_sent else "EMAIL_IN",
                        "text":     msg["subject"],
                        "date":     msg["date"],
                        "user":     acc_email,
                        "from":     msg["from"],
                        "to":       msg["to"],
                    }
                    contact_entries.setdefault(match_e, []).append(entry)
        finally:
            try:
                conn.logout()
            except Exception:
                pass

        # Write — ArrayUnion is idempotent (same email_id = same map = deduped)
        for match_e, entries in contact_entries.items():
            ref = contact_index[match_e]
            try:
                update_doc: dict = {"comment_history": firestore.ArrayUnion(entries)}
                # Set new_mail flag if any incoming email was added
                has_incoming = any(e.get("type") == "EMAIL_IN" for e in entries)
                if has_incoming:
                    update_doc["new_mail"] = True
                ref.update(update_doc)
                total_entries  += len(entries)
                total_contacts += 1
                print(f"  {match_e}: +{len(entries)} entries", flush=True)
            except Exception as exc:
                errors.append(f"{match_e}: write failed — {exc}")

    print(f"[followup-email-sync] done — {total_entries} entries / {total_contacts} contacts", flush=True)
    return {
        "synced_entries":  total_entries,
        "synced_contacts": total_contacts,
        "days":            days,
        "errors":          errors,
    }
