"""app/reply_match.py — CLI for the reply-matching pipeline.

Fetches emails directly from IMAP, classifies them, and matches replies/bounces
to campaign contacts in a single pass.  inbox_messages is written as audit log only.

Examples
--------
# Full run — all accounts, all campaigns, last 30 days
python app/reply_match.py

# Dry-run — show what would match without writing anything
python app/reply_match.py --dry-run

# Specific accounts only
python app/reply_match.py --accounts sales@blueboot.ai info@blueboot.ai

# Specific campaigns only
python app/reply_match.py --campaigns NO_jun SE_jun

# Look back further
python app/reply_match.py --days 90

# Limit messages per account
python app/reply_match.py --limit 50 --dry-run
"""
from __future__ import annotations

import argparse
import sys
import os
import re

# ── path setup ────────────────────────────────────────────────────────────────
_HERE   = os.path.dirname(os.path.abspath(__file__))
_FCRM   = os.path.join(os.path.dirname(_HERE), "functions-crm")
for _p in [_HERE, _FCRM]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _pathsetup  # noqa: F401  sets Windows Selector event loop + project root


def _split_list(values: list[str]) -> list[str]:
    """Accept space- or comma/semicolon/pipe-separated items."""
    out: list[str] = []
    for v in values:
        out.extend(x.strip() for x in re.split(r"[,;|\s]+", v) if x.strip())
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Fetch IMAP replies and match them to campaign contacts."
    )
    parser.add_argument(
        "--accounts", nargs="+", metavar="EMAIL",
        help="IMAP accounts to fetch (default: all accounts in settings)"
    )
    parser.add_argument(
        "--campaigns", nargs="+", metavar="ID",
        help="Campaign IDs to restrict matching to (default: all)"
    )
    parser.add_argument(
        "--limit", type=int, default=200, metavar="N",
        help="Max inbox_messages to process in the match loop (default: 200)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch + find matches but write nothing"
    )
    parser.add_argument(
        "--days", type=int, default=30, metavar="N",
        help="How many days back to search IMAP (default: 30)"
    )
    args = parser.parse_args(argv)

    accounts  = _split_list(args.accounts)  if args.accounts  else None
    campaigns = _split_list(args.campaigns) if args.campaigns else None

    print("=" * 60)
    print("Reply Match Pipeline")
    if args.dry_run:
        print("  MODE: DRY RUN — no writes")
    if accounts:
        print(f"  Accounts:  {accounts}")
    if campaigns:
        print(f"  Campaigns: {campaigns}")
    print(f"  Limit:     {args.limit}")
    print(f"  Lookback:  {args.days} days")
    print("=" * 60)

    # Initialize Firebase with the local service-account key so that the
    # functions-crm firestore client reuses the same app (it checks
    # firebase_admin._apps before calling ApplicationDefault).
    from firestore_client import get_firestore
    get_firestore()

    from smart_mail.reply_matcher import match_new_replies

    result = match_new_replies(
        limit     = args.limit,
        accounts  = accounts,
        campaigns = campaigns,
        dry_run   = args.dry_run,
        days      = args.days,
    )

    print()
    print("=" * 60)
    print("Summary")
    print(f"  Accounts checked : {result.get('accounts_checked', '-')}")
    print(f"  Accounts failed  : {result.get('accounts_failed', '-')}")
    print(f"  Replies seen     : {result.get('replies', '-')}")
    print(f"  Bounces seen     : {result.get('bounces', '-')}")
    print(f"  DMARC reports    : {result.get('dmarc', '-')}")
    print(f"  Replies matched  : {result.get('matched', 0)}")
    print(f"  Bounces matched  : {result.get('bounced', 0)}")
    print(f"  Unmatched        : {result.get('unmatched', 0)}")
    print(f"  Skipped (system) : {result.get('skipped', 0)}")
    print(f"  Errors           : {result.get('errors', 0)}")
    if args.dry_run:
        print("  (dry-run — nothing was written)")
    print("=" * 60)


if __name__ == "__main__":
    main()
