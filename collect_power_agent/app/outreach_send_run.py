# app/outreach_send_run.py
"""Run the outreach sender from the command line.

By default this is a dry run: it uses the same smart sender selection and
rendering path as a real send, but does not open a mail account, send mail, or
call confirm_sent(). Add --send to dispatch real mail and write confirmations.

Usage examples
--------------
  # Dry-run intro mode
  python app/outreach_send_run.py

  # Dry-run followup mode with rendered body snippet
  python app/outreach_send_run.py --mode followup --preview

  # Send real intro mails
  python app/outreach_send_run.py --send --mode intro

  # Send both intro and followup passes
  python app/outreach_send_run.py --send --mode both

  # Filter to one campaign
  python app/outreach_send_run.py --campaign ram-test1

  # Cap contacts fetched
  python app/outreach_send_run.py --limit 20

  # List all campaign IDs and exit
  python app/outreach_send_run.py --list-campaigns
"""
from __future__ import annotations

import os
import sys

import _pathsetup  # noqa: F401 -- sets up Windows event loop policy + path


_FUNCTIONS_SMARTMAIL = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "functions-smartmail")
)
if _FUNCTIONS_SMARTMAIL not in sys.path:
    sys.path.insert(0, _FUNCTIONS_SMARTMAIL)


def _get_db():
    from firestore_client import get_firestore
    return get_firestore()


def _list_campaigns(db) -> list[str]:
    return sorted(d.id for d in db.collection("campaigns").stream())


def _run(args) -> None:
    from smart_mail.smart_campaign_sender import send_outreach

    dry_run = not args.send
    modes = ["intro", "followup"] if args.mode == "both" else [args.mode]

    print(
        "[%s] outreach send loop  mode=%s  limit=%d"
        % ("dry-run" if dry_run else "live", args.mode, args.limit)
    )
    if args.campaign:
        print("[%s] filtering to campaign: %s" % ("dry-run" if dry_run else "live", args.campaign))

    summaries = {}
    for mode in modes:
        summaries[mode] = send_outreach(
            mode=mode,
            limit=args.limit,
            campaign_id=args.campaign,
            dry_run=dry_run,
            preview=args.preview,
        )

    print("\n[summary]")
    for mode, summary in summaries.items():
        print(
            "  %s: sent=%d  would_send=%d  failed=%d  skipped=%d"
            % (
                mode,
                summary.get("sent", 0),
                summary.get("would_send", 0),
                summary.get("failed", 0),
                summary.get("skipped", 0),
            )
        )
    if dry_run:
        print("  nothing sent, nothing written")


def main(argv=None) -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    import argparse

    parser = argparse.ArgumentParser(
        description="Run the outreach sender. Defaults to dry-run; add --send for real mail.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage examples")[-1] if "Usage examples" in __doc__ else "",
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["intro", "followup", "both"],
        default="intro",
        help="intro, followup, or both passes",
    )
    parser.add_argument(
        "--campaign", "-c",
        default=None,
        metavar="CAMPAIGN_ID",
        help="Only process contacts in this campaign",
    )
    parser.add_argument(
        "--limit", "-n",
        type=int,
        default=500,
        metavar="N",
        help="Maximum total contacts to fetch per mode",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="In dry-run mode, show rendered body snippets",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="Send real mail and call confirm_sent(). Without this flag, this command is dry-run only.",
    )
    parser.add_argument(
        "--list-campaigns",
        action="store_true",
        help="Print all campaign IDs and exit",
    )

    args = parser.parse_args(argv)

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

    _run(args)


if __name__ == "__main__":
    main()
