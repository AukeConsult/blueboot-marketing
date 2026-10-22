# app/check_sent_totals.py
"""Read-only reconciliation: mails recorded on contacts (mail_sent) versus the send log (outreach_sent).

The Outreach send/check tile "Mails sent" adds up mail_sent over the contacts of ready/active campaigns.
The send log (outreach_sent) has one document per mail that left. This script matches the two by
message_id and prints per campaign:
  contacts  = mails recorded on contacts
  log       = mails in the send log
  both      = mails found in both
  only c    = on a contact but not in the log   (sent before the log existed, or the log was deleted by a reset)
  only log  = in the log but not on a contact   (contact deleted/moved, or the contact update failed)
Nothing is written.

Usage:
  python app/check_sent_totals.py                      # all campaigns
  python app/check_sent_totals.py --active-only        # only ready/active, like the tile
  python app/check_sent_totals.py --campaigns DK_WORDPRESS,SE_WORDPRESS
"""
from __future__ import annotations

import argparse
import re
from collections import defaultdict

import _pathsetup  # noqa: F401 -- sets up Windows event loop policy + path


def reconcile(contact_mails: dict, log_mails: dict) -> dict:
    """contact_mails / log_mails: {campaign_id: set(message_id)} -> per-campaign counts and a total row."""
    rows, total = {}, defaultdict(int)
    for cid in sorted(set(contact_mails) | set(log_mails)):
        c, l = contact_mails.get(cid, set()), log_mails.get(cid, set())
        row = {"contacts": len(c), "log": len(l), "both": len(c & l),
               "only_contacts": len(c - l), "only_log": len(l - c)}
        rows[cid] = row
        for k, v in row.items():
            total[k] += v
    return {"rows": rows, "total": dict(total)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--campaigns", default="", help="comma separated campaign ids (default: all)")
    ap.add_argument("--active-only", action="store_true", help="only campaigns with status ready or active (what the tile counts)")
    args = ap.parse_args()

    from firestore_client import get_firestore
    db = get_firestore()
    wanted = {c.strip() for c in re.split(r"[,;|\s]+", args.campaigns) if c.strip()}

    campaigns = {}
    for d in db.collection("campaigns").stream():
        if wanted and d.id not in wanted:
            continue
        st = str((d.to_dict() or {}).get("status", "")).strip().lower()
        if args.active_only and st not in ("ready", "active"):
            continue
        campaigns[d.id] = st
    print(f"Campaigns checked: {len(campaigns)}")

    contact_mails: dict[str, set] = defaultdict(set)
    raw_entries = 0          # every mail_sent entry, duplicates included: what the tile adds up
    no_id = 0
    for cid in campaigns:
        for doc in db.collection("campaigns").document(cid).collection("campaign_contacts").select(["mail_sent"]).stream():
            for m in (doc.to_dict() or {}).get("mail_sent") or []:
                raw_entries += 1
                mid = str((m or {}).get("message_id") or "").strip() if isinstance(m, dict) else ""
                if mid:
                    contact_mails[cid].add(mid)
                else:
                    no_id += 1
                    contact_mails[cid].add(f"noid:{cid}:{doc.id}:{(m or {}).get('sent_at', '')}")

    log_mails: dict[str, set] = defaultdict(set)
    log_total = 0
    for doc in db.collection("outreach_sent").stream():
        d = doc.to_dict() or {}
        cid = d.get("campaign_id", "")
        if cid not in campaigns:
            continue
        log_total += 1
        log_mails[cid].add(str(d.get("message_id") or "").strip() or f"noid-log:{doc.id}")

    res = reconcile(contact_mails, log_mails)
    print(f"\n{'campaign':28} {'status':9} {'contacts':>9} {'log':>6} {'both':>6} {'only c':>7} {'only log':>9}")
    for cid, r in res["rows"].items():
        flag = "" if r["only_contacts"] == 0 and r["only_log"] == 0 else "   <-- differs"
        print(f"{cid[:28]:28} {campaigns.get(cid, ''):9} {r['contacts']:>9} {r['log']:>6} {r['both']:>6} "
              f"{r['only_contacts']:>7} {r['only_log']:>9}{flag}")
    t = res["total"]
    print(f"\nTOTAL  mails on contacts: {t.get('contacts', 0)}   in send log: {t.get('log', 0)}   "
          f"in both: {t.get('both', 0)}   only on contacts: {t.get('only_contacts', 0)}   only in log: {t.get('only_log', 0)}")
    print(f"Raw mail_sent entries (what the tile adds up): {raw_entries}")
    if raw_entries != t.get('contacts', 0):
        print(f"  -> {raw_entries - t.get('contacts', 0)} entr(ies) share a message_id with another entry: the same mail recorded twice.")
    if no_id:
        print(f"Note: {no_id} mail(s) on contacts have no message_id, matched by contact and time only.")
    print("\nThe tile 'Mails sent, all time' should equal the 'contacts' total (for ready/active campaigns).")


if __name__ == "__main__":
    main()
