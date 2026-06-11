# functions-smartmail/smart_mail/smart_reply_matcher.py
# Adapted copy of app/smart-mail-not-in-use/smart_reply_matcher.py -- logic identical
# (Message-ID threading match -> from_email fallback -> mark + refresh
# stats); only the two imports are relative and the CLI __main__ block is
# dropped (main.py's /run-replies route is the entry point here).

import re

from datetime import datetime, timezone

from .firestore_client import get_firestore
from .smart_campaign_stats import refresh_campaign_stats


_MSGID_RE = re.compile(r"<[^<>\s]+>")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _extract_message_ids(*headers):
    ids = []
    seen = set()
    for header in headers:
        if not header:
            continue
        for token in _MSGID_RE.findall(header):
            if token not in seen:
                seen.add(token)
                ids.append(token)
    return ids


def _bare_email(value: str) -> str:
    if not value:
        return ""
    m = _EMAIL_RE.search(value)
    return m.group(0).lower() if m else ""


def _find_outreach_by_message_id(db, message_ids):
    for mid in message_ids:
        docs = list(
            db.collection("outreach_sent")
            .where("message_id", "==", mid)
            .limit(1)
            .stream()
        )
        if docs:
            return docs[0].to_dict(), "message_id"
    return None, None


def _find_outreach_by_from_email(db, from_email):
    """NOTE: requires a composite index on outreach_sent (to_email ASC, sent_at DESC)."""
    addr = _bare_email(from_email)
    if not addr:
        return None, None

    try:
        docs = list(
            db.collection("outreach_sent")
            .where("to_email", "==", addr)
            .order_by("sent_at", direction="DESCENDING")
            .limit(1)
            .stream()
        )
    except Exception as ex:
        print(f"[reply_matcher] from_email lookup failed for {addr!r}: {ex}")
        return None, None

    if docs:
        return docs[0].to_dict(), "from_email"
    return None, None


def _mark_message(db, message_doc_id, payload):
    db.collection("inbox_messages").document(message_doc_id).update(payload)


def _apply_reply(db, outreach: dict, message: dict, matched_via: str):
    campaign_id = outreach.get("campaign_id")
    contact_doc_id = outreach.get("contact_doc_id")
    received_at = message.get("received_at") or datetime.now(timezone.utc).isoformat()
    snippet = (message.get("body_text") or "")[:2000]

    reply_payload = {
        "status": "active",
        "followup_status": "replied",
        "new_mail": True,
        "replied_at": received_at,
        "reply_snippet": snippet,
        "reply_subject": message.get("subject"),
        "reply_from": message.get("from_email"),
        "matched_via": matched_via,
    }

    if not (campaign_id and contact_doc_id):
        return campaign_id, contact_doc_id

    try:
        (
            db.collection("campaigns")
            .document(campaign_id)
            .collection("campaign_contacts")
            .document(contact_doc_id)
            .update(reply_payload)
        )
    except Exception as ex:
        print(f"[reply_matcher] could not update campaign_contacts {campaign_id}/{contact_doc_id}: {ex}")

    try:
        db.collection("email_contacts").document(contact_doc_id).update(reply_payload)
    except Exception as ex:
        print(f"[reply_matcher] could not update email_contacts {contact_doc_id}: {ex}")

    try:
        refresh_campaign_stats(campaign_id)
    except Exception as ex:
        print(f"[reply_matcher] could not refresh stats for {campaign_id}: {ex}")

    return campaign_id, contact_doc_id


def match_new_replies(limit: int = 200) -> dict:
    """Process unmatched inbox_messages once; never raises. Returns a summary dict."""
    db = get_firestore()
    summary = {"checked": 0, "matched": 0, "unmatched": 0, "errors": 0}

    docs = list(
        db.collection("inbox_messages")
        .where("reply_matched", "==", False)
        .limit(limit)
        .stream()
    )

    for doc in docs:
        summary["checked"] += 1
        message = doc.to_dict()

        try:
            message_ids = _extract_message_ids(
                message.get("in_reply_to"), message.get("references")
            )

            outreach, matched_via = _find_outreach_by_message_id(db, message_ids)
            if outreach is None:
                outreach, matched_via = _find_outreach_by_from_email(db, message.get("from_email"))

            if outreach is not None:
                campaign_id, contact_doc_id = _apply_reply(db, outreach, message, matched_via)
                _mark_message(
                    db,
                    doc.id,
                    {
                        "reply_matched": True,
                        "match_status": "matched",
                        "matched_via": matched_via,
                        "matched_campaign_id": campaign_id,
                        "matched_contact_doc_id": contact_doc_id,
                        "matched_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                summary["matched"] += 1
                print(f"[reply_matcher] matched {doc.id} -> campaign={campaign_id} contact={contact_doc_id} via={matched_via}")
            else:
                _mark_message(
                    db,
                    doc.id,
                    {
                        "reply_matched": True,
                        "match_status": "unmatched",
                        "matched_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                summary["unmatched"] += 1
                print(f"[reply_matcher] no match for {doc.id} (from={message.get('from_email')!r})")

        except Exception as ex:
            summary["errors"] += 1
            print(f"[reply_matcher] error processing {doc.id}: {ex}")
            try:
                _mark_message(
                    db,
                    doc.id,
                    {
                        "reply_matched": True,
                        "match_status": "error",
                        "match_error": str(ex),
                        "matched_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            except Exception:
                pass

    return summary
