# app/smart_mail/smart_reply_matcher.py
"""
Matches inbound replies (inbox_messages, written by app.mail_reader) to
outbound campaign sends (outreach_sent / campaign_contacts) and marks the
contact as replied.

Matching strategy, in order of confidence:
  1. Message-ID threading -- parse In-Reply-To / References headers from the
     reply and look up an outreach_sent doc whose message_id appears among
     them. This is the most reliable signal: RFC 5322 threading headers
     survive forwarding/quoting/display-name changes.
  2. from_email fallback -- if no header match, look up the most recent
     outreach_sent to that bare address. Less precise (a contact could reply
     from a different address, or have been targeted by more than one
     campaign) but far better than dropping the reply.

On a match: campaign_contacts + email_contacts -> status="replied" with the
reply snippet/subject/received_at, campaign stats are refreshed (the existing
`replied` counter slot in smart_campaign_stats.py starts getting populated),
and the inbox_message is flagged so it is never reprocessed. Unmatched
messages are flagged too (match_status="unmatched") so the worker doesn't
spin on the same handful of stray emails forever.
"""
import argparse
import re

from datetime import datetime, timezone

from app.firestore_client import get_firestore
from smart_campaign_stats import refresh_campaign_stats


_MSGID_RE = re.compile(r"<[^<>\s]+>")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _extract_message_ids(*headers):
    """Pull every <...> Message-ID token out of In-Reply-To/References headers."""
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
    """Extract the bare address out of a 'Display Name <addr>' From header."""
    if not value:
        return ""
    m = _EMAIL_RE.search(value)
    return m.group(0).lower() if m else ""


def _find_outreach_by_message_id(db, message_ids):
    """Return (outreach_dict, 'message_id') for the first header match, else (None, None)."""
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
    """
    Fallback: most recent outreach_sent to this bare address.

    NOTE: requires a composite index on outreach_sent (to_email ASC,
    sent_at DESC) -- Firestore will print a console link to create it the
    first time this query runs if it's missing.
    """
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
    """Flip the matched campaign_contact + email_contact to status=replied."""
    campaign_id = outreach.get("campaign_id")
    contact_doc_id = outreach.get("contact_doc_id")
    received_at = message.get("received_at") or datetime.now(timezone.utc).isoformat()
    snippet = (message.get("body_text") or "")[:2000]

    reply_payload = {
        "status": "replied",
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
        print(f"[reply_matcher] could not update campaign_contacts "
              f"{campaign_id}/{contact_doc_id}: {ex}")

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
    """
    Process unmatched inbox_messages once and return a small summary dict
    ({"checked", "matched", "unmatched", "errors"}).

    Never raises -- one bad message must not lose the rest of the batch or
    crash the polling worker. Each message is wrapped in its own try/except
    and always ends up flagged (reply_matched=True) so the queue drains even
    when individual lookups fail.
    """
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
                outreach, matched_via = _find_outreach_by_from_email(
                    db, message.get("from_email")
                )

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
                print(f"[reply_matcher] matched {doc.id} -> "
                      f"campaign={campaign_id} contact={contact_doc_id} via={matched_via}")
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
                print(f"[reply_matcher] no match for {doc.id} "
                      f"(from={message.get('from_email')!r})")

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


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()

    result = match_new_replies(limit=args.limit)
    print(f"[reply_matcher] done: {result}")
