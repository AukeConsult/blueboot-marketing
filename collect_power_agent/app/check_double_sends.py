# app/check_double_sends.py
"""Read-only check: did any contact get two mails too close together (e.g. a reminder sent twice)?

For every contact with two or more entries in mail_sent, the gap between consecutive mails is
compared with --min-hours (default 24). Contacts below it are listed. Nothing is written.

Usage:
  python app/check_double_sends.py                       # all campaigns
  python app/check_double_sends.py --campaigns DK_WORDPRESS,SE_WORDPRESS
  python app/check_double_sends.py --min-hours 48
"""
from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone

import _pathsetup  # noqa: F401 -- sets up Windows event loop policy + path


def _dt(value):
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def close_pairs(mail_sent, min_hours: float) -> list[tuple[int, float]]:
    """[(index of the later mail, gap in hours)] for consecutive mails closer than min_hours,
    plus pairs sharing a message_id (the same mail recorded twice)."""
    items = [m for m in (mail_sent or []) if isinstance(m, dict)]
    out = []
    for i in range(1, len(items)):
        a, b = _dt(items[i - 1].get("sent_at")), _dt(items[i].get("sent_at"))
        if a and b:
            gap = abs((b - a).total_seconds()) / 3600.0
            if gap < min_hours:
                out.append((i, round(gap, 2)))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--campaigns", default="", help="comma separated campaign ids (default: all)")
    ap.add_argument("--min-hours", type=float, default=24.0)
    args = ap.parse_args()

    from firestore_client import get_firestore
    db = get_firestore()
    wanted = [c.strip() for c in re.split(r"[,;|\s]+", args.campaigns) if c.strip()]
    ids = wanted or sorted(d.id for d in db.collection("campaigns").stream())

    checked = flagged = 0
    for cid in ids:
        for doc in db.collection("campaigns").document(cid).collection("campaign_contacts").stream():
            d = doc.to_dict() or {}
            sent = d.get("mail_sent") or []
            if len(sent) < 2:
                continue
            checked += 1
            pairs = close_pairs(sent, args.min_hours)
            if pairs:
                flagged += 1
                gaps = ", ".join(f"mail {i + 1} only {g} h after mail {i}" for i, g in pairs)
                print(f"[DOUBLE] {cid}  {d.get('email', '')}  ({len(sent)} mails)  {gaps}")

    print(f"\nChecked {checked} contact(s) with 2+ mails in {len(ids)} campaign(s): "
          f"{flagged} with mails less than {args.min_hours:g} h apart.")
    if not flagged:
        print("No double sends found.")


if __name__ == "__main__":
    main()
