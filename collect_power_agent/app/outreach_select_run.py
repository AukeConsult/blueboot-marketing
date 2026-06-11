# app/outreach_select_run.py
"""Dry-run the outreach mail select -- shows who would be sent to and what.

Calls read_outreach() from functions-smartmail/outreach_mail_select.py and
prints the resolved batches without sending or writing anything to Firestore.

Usage examples
--------------
  # Dry-run intro mode (pending contacts) across all campaigns
  python app/outreach_select_run.py

  # Dry-run followup mode
  python app/outreach_select_run.py --mode followup

  # Limit output to one campaign
  python app/outreach_select_run.py --campaign NO_jun

  # Limit total contacts shown
  python app/outreach_select_run.py --limit 50

  # List all campaign IDs and exit
  python app/outreach_select_run.py --list-campaigns
"""
from __future__ import annotations

import os
import sys

import _pathsetup  # noqa: F401 -- sets up Windows event loop policy + path

# Make functions-smartmail importable
_FUNCTIONS_SMARTMAIL = os.path.join(os.path.dirname(__file__), "..", "functions-smartmail")
if _FUNCTIONS_SMARTMAIL not in sys.path:
    sys.path.insert(0, os.path.abspath(_FUNCTIONS_SMARTMAIL))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_db():
    from firestore_client import get_firestore
    return get_firestore()


def _list_campaigns(db) -> list[str]:
    return sorted(d.id for d in db.collection("campaigns").stream())


def _print_batch(batch, campaign_filter, verbose) -> int:
    """Print one AccountBatch; return number of contacts printed."""
    printed = 0
    for cwc in batch.campaigns:
        cid = cwc.campaign.campaign_id
        if campaign_filter and cid != campaign_filter:
            continue
        contacts = cwc.contacts
        print(
            "\n  Campaign : %s  (%s)  status=%s  contacts=%d"
            % (cid, cwc.campaign.campaign_name, cwc.campaign.status, len(contacts))
        )
        if verbose:
            print("    subject : %r" % cwc.campaign.subject_template)
        for c in contacts:
            mail_sent_count = len(c.mail_sent or [])
            selected_idx = c.next_mail_index
            in_reply_to = c.in_reply_to
            line = "    %-35s  %-25s  %-4s  sent=%d  next_idx=%d" % (
                c.email, c.company, c.country, mail_sent_count, selected_idx,
            )
            if in_reply_to:
                line += "  reply_to=<...%s>" % in_reply_to[-12:]
            print(line)
            printed += 1
    return printed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    import argparse
    p = argparse.ArgumentParser(
        description="Dry-run the outreach mail select -- shows resolved batches without sending.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage examples")[-1] if "Usage examples" in __doc__ else "",
    )
    p.add_argument(
        "--mode", "-m",
        choices=["intro", "followup"],
        default="intro",
        help="intro = pending contacts with no sent mail; followup = pending contacts with a due sequence step",
    )
    p.add_argument(
        "--campaign", "-c",
        default=None,
        metavar="CAMPAIGN_ID",
        help="Only show contacts in this campaign (default: all campaigns)",
    )
    p.add_argument(
        "--limit", "-n",
        type=int,
        default=500,
        metavar="N",
        help="Maximum total contacts to fetch (default: 500)",
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Also print subject template for each campaign",
    )
    p.add_argument(
        "--list-campaigns",
        action="store_true",
        help="Print all campaign IDs and exit",
    )

    args = p.parse_args(argv)

    db = _get_db()

    if args.list_campaigns:
        ids = _list_campaigns(db)
        if ids:
            print("Available campaigns:")
            for cid in ids:
                print("  %s" % cid)
        else:
            print("No campaigns found.")
        return

    from outreach_mail_select import read_outreach

    print("[dry-run] read_outreach  mode=%s  limit=%d" % (args.mode, args.limit))
    if args.campaign:
        print("[dry-run] filtering to campaign: %s" % args.campaign)

    batches = read_outreach(mode=args.mode, limit=args.limit)

    if not batches:
        print("No outreach candidates found.")
        return

    total_contacts = 0
    total_campaigns = 0

    for batch in batches:
        acc = batch.account
        matching = [
            cwc for cwc in batch.campaigns
            if not args.campaign or cwc.campaign.campaign_id == args.campaign
        ]
        if not matching:
            continue
        n_contacts = sum(len(cwc.contacts) for cwc in matching)
        print(
            "\nAccount : %s  (%s)  host=%s:%d  %s  campaigns=%d  contacts=%d"
            % (
                acc.email, acc.from_name, acc.host, acc.port,
                "SSL" if acc.use_ssl else "STARTTLS",
                len(matching), n_contacts,
            )
        )
        printed = _print_batch(batch, args.campaign, args.verbose)
        total_contacts += printed
        total_campaigns += len(matching)

    print(
        "\n[dry-run] total  accounts=%d  campaigns=%d  contacts=%d  (nothing written)"
        % (len(batches), total_campaigns, total_contacts)
    )


if __name__ == "__main__":
    main()
