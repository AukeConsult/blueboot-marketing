# app/followup_email_sync.py
"""Command-line runner for the follow-up email sync.

Fetches inbox + sent messages for each outreach mail account, matches them
against campaign_contacts by email address, and appends EMAIL_IN / EMAIL_OUT
entries to each matching contact's comment_history in Firestore.

The operation is idempotent: each entry carries a unique email_id so running
the sync multiple times against the same mailbox never creates duplicates.

Usage examples
--------------
  # Sync all contacts, last 7 days (default)
  python app/followup_email_sync.py

  # Sync last 30 days for all contacts
  python app/followup_email_sync.py --days 30

  # Sync all time for one or more campaigns
  python app/followup_email_sync.py --campaigns NO_jun SE_jun --days 0
  python app/followup_email_sync.py --campaigns NO_jun,SE_jun --days 0

  # Sync one specific contact
  python app/followup_email_sync.py --campaigns NO_jun --contact john_doe_example_com

  # Preview what would be synced without writing to Firestore
  python app/followup_email_sync.py --dry-run
"""
from __future__ import annotations

import os
import re
import sys

import _pathsetup  # noqa: F401 — sets up Windows event loop policy + path

# Make the smart-mail package importable so we can reuse the shared mail sync lib.
_FUNCTIONS_SMARTMAIL = os.path.join(os.path.dirname(__file__), "..", "functions-smartmail")
if _FUNCTIONS_SMARTMAIL not in sys.path:
    sys.path.insert(0, os.path.abspath(_FUNCTIONS_SMARTMAIL))

from smart_mail.followup_email_sync_lib import run_followup_email_sync


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_db():
    """Return a Firestore client using the local service-account credential."""
    from firestore_client import get_firestore
    return get_firestore()


def _list_campaigns(db) -> list[str]:
    return sorted(d.id for d in db.collection("campaigns").stream())


# ── CLI ───────────────────────────────────────────────────────────────────────

def _split_list_arg(values) -> list[str]:
    if not values:
        return []
    out: list[str] = []
    for item in values:
        out.extend(v.strip() for v in re.split(r"[,;|\n]", str(item)) if v.strip())
    return out


def main(argv=None) -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    import argparse
    p = argparse.ArgumentParser(
        description="Sync email history (inbox + sent) into campaign contact follow-up logs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage examples")[1] if "Usage examples" in __doc__ else "",
    )
    p.add_argument(
        "--campaigns", "-c",
        nargs="+",
        default=None,
        metavar="CAMPAIGN_ID",
        help="Only sync contacts in these campaign IDs. Accepts space, comma, semicolon, or pipe separated values. Default: all campaigns.",
    )
    p.add_argument(
        "--contact", "-d",
        default=None,
        metavar="DOC_ID",
        help="Only sync this specific contact doc ID (requires exactly one --campaigns value)",
    )
    p.add_argument(
        "--days", "-n",
        type=int,
        default=7,
        metavar="N",
        help="Lookback window in days (default: 7, use 0 for all time)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and match emails but do NOT write to Firestore",
    )
    p.add_argument(
        "--list-campaigns",
        action="store_true",
        help="Print all campaign IDs and exit",
    )

    args = p.parse_args(argv)
    campaign_ids = _split_list_arg(args.campaigns)

    if args.contact and len(campaign_ids) != 1:
        p.error("--contact requires exactly one --campaigns value")

    db = _get_db()

    if args.list_campaigns:
        ids = _list_campaigns(db)
        if ids:
            print("Available campaigns:")
            for cid in ids:
                print(f"  {cid}")
        else:
            print("No campaigns found.")
        return

    # ── Print run config ──────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("  FOLLOW-UP EMAIL SYNC")
    print("=" * 60)
    scope = ", ".join(campaign_ids) if campaign_ids else "ALL campaigns"
    if args.contact:
        scope += f"  /  contact: {args.contact}"
    window = f"last {args.days} day{'s' if args.days != 1 else ''}" if args.days else "all time"
    print(f"  Scope  : {scope}")
    print(f"  Window : {window}")
    print(f"  Mode   : {'DRY RUN (no writes)' if args.dry_run else 'LIVE'}")
    print("=" * 60)
    print()

    if args.dry_run:
        # Dry run: run the lib with a fake db that intercepts ArrayUnion writes
        _run_dry(db, args)
    else:
        result = run_followup_email_sync(
            db             = db,
            campaign_ids   = campaign_ids,
            contact_doc_id = args.contact  or None,
            days           = args.days,
        )
        _print_result(result)


def _run_dry(db, args):
    """Dry run: import the matching logic, skip the Firestore writes."""
    import re
    from smart_mail.followup_email_sync_lib import (
        _imap_connect, _find_sent_folder, _fetch_headers,
        _extract_email, _history_email_ids, _msg_key,
        CAMPAIGNS_COLLECTION, CONTACTS_SUBCOLLECTION,
        SETTINGS_COLLECTION, MAIL_ACCOUNTS_DOC,
    )
    from datetime import datetime, timedelta, timezone

    now    = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=args.days)) if args.days > 0 else None
    campaign_ids = _split_list_arg(args.campaigns)

    ma_col  = db.collection(SETTINGS_COLLECTION).document(MAIL_ACCOUNTS_DOC).collection("accounts")
    all_mas = {d.id: d.to_dict() for d in ma_col.stream()}
    if not all_mas:
        print("No mail accounts configured — nothing to sync.")
        return

    camps_col = db.collection(CAMPAIGNS_COLLECTION)
    account_contacts: dict[str, list] = {acc: [] for acc in all_mas}

    if args.contact:
        campaign_id = campaign_ids[0]
        ref  = camps_col.document(campaign_id).collection(CONTACTS_SUBCOLLECTION).document(args.contact)
        snap = ref.get()
        if snap.exists:
            c  = snap.to_dict() or {}
            cd = camps_col.document(campaign_id).get().to_dict() or {}
            acc = (cd.get("outreach_email_account") or "").strip().lower()
            if acc in account_contacts:
                account_contacts[acc].append((campaign_id, args.contact, c.get("email", ""), ref))
    elif campaign_ids:
        for campaign_id in campaign_ids:
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

    total = 0
    would_update_contacts: set[str] = set()
    for acc_email, contacts in account_contacts.items():
        if not contacts:
            continue
        ma = all_mas[acc_email]
        contact_index = {c[2].lower(): c for c in contacts if c[2]}
        if not contact_index:
            continue
        contact_seen_ids: dict[str, set[str]] = {}

        print(f"[{acc_email}] connecting…")
        try:
            conn = _imap_connect(ma, acc_email)
        except Exception as exc:
            print(f"  ERROR connecting: {exc}")
            continue

        try:
            sent_folder  = _find_sent_folder(conn)
            folders_dirs = [("INBOX", False)] + ([(sent_folder, True)] if sent_folder else [])
            for folder, is_sent in folders_dirs:
                for msg in _fetch_headers(conn, folder, cutoff, 500):
                    from_addr = _extract_email(msg["from"])
                    to_addrs  = [_extract_email(a) for a in re.split(r"[,;]", msg["to"])]
                    match_e   = to_addrs[0] if is_sent else from_addr
                    if not match_e or match_e not in contact_index:
                        continue
                    msg_key = _msg_key(msg["message_id"], folder, msg["uid"])
                    if match_e not in contact_seen_ids:
                        ref = contact_index[match_e][3]
                        snap = ref.get()
                        contact_seen_ids[match_e] = _history_email_ids(
                            (snap.to_dict() or {}).get("comment_history") if snap.exists else []
                        )
                    if msg_key in contact_seen_ids[match_e]:
                        continue
                    contact_seen_ids[match_e].add(msg_key)
                    direction = "OUT" if is_sent else "IN "
                    print(f"  [{direction}] {msg['date'][:10]}  {match_e:<35}  {msg['subject'][:50]}")
                    total += 1
                    would_update_contacts.add(match_e)
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    print()
    print(
        f"  Dry run complete — {total} email(s) would be synced; "
        f"{len(would_update_contacts)} contact(s) would be updated (no writes made)"
    )
    print()


def _print_result(result: dict) -> None:
    print()
    print("=" * 60)
    print("  SYNC COMPLETE")
    print("=" * 60)
    print(f"  New entries    : {result.get('synced_entries', 0)}")
    print(f"  Contacts hit   : {result.get('synced_contacts', 0)}")
    print(f"  Contacts updated: {result.get('updated_contacts', result.get('synced_contacts', 0))}")
    print(f"  Window         : {result.get('days', '?')} days")
    errors = result.get("errors") or []
    if errors:
        print(f"  Errors ({len(errors)}):")
        for e in errors:
            print(f"    ! {e}")
    print("=" * 60)
    print()


if __name__ == "__main__":
    main()
