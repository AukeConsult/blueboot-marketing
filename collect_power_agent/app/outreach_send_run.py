# app/outreach_send_run.py
"""Dry-run the outreach SEND loop -- renders each mail without sending anything.

One step beyond outreach_select_run.py: resolves the exact mail-sequence step
each contact would receive (based on their mail_sent history), renders subject
and body via render_mail(), checks SMTP password availability, and shows the
send budget per account -- without sending a single email or writing to Firestore.

Usage examples
--------------
  # Intro mode -- all campaigns (default)
  python app/outreach_send_run.py

  # Followup mode
  python app/outreach_send_run.py --mode followup

  # Filter to one campaign
  python app/outreach_send_run.py --campaign ram-test1

  # Show rendered subject + body snippet per contact
  python app/outreach_send_run.py --preview

  # Cap contacts fetched
  python app/outreach_send_run.py --limit 20

  # List all campaign IDs and exit
  python app/outreach_send_run.py --list-campaigns
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


def _get_smtp_password_envvar(account_email: str) -> str:
    """Return the env var name for an account's SMTP password (does not read the value)."""
    alias = account_email.split("@")[0].upper()
    return f"{alias}_SMTP_PASSWORD"


def _password_available(account_email: str) -> bool:
    return bool(os.getenv(_get_smtp_password_envvar(account_email), ""))


# ---------------------------------------------------------------------------
# Per-contact render + display
# ---------------------------------------------------------------------------

def _render_contact(step, contact, show_preview: bool) -> None:
    """Render the mail for one contact and print a summary line."""
    from outreach_render_mail import render_mail

    rendered = render_mail(step, contact)

    # Summary line: email | company | step | subject
    line = "    %-35s  %-20s  [step %d: %s]  subj: %r" % (
        contact.email,
        (contact.company or "")[:20],
        step.index,
        step.mail_type,
        rendered.subject[:60],
    )
    print(line)

    if show_preview:
        body = (rendered.html_body or rendered.text_body or "").strip()
        # Show first 200 chars of body, collapsed to single lines
        snippet = " ".join(body[:200].split())
        if len(body) > 200:
            snippet += " ..."
        print("      body: %s" % snippet)


# ---------------------------------------------------------------------------
# Main dry-run logic
# ---------------------------------------------------------------------------

def _run(args) -> None:
    from outreach_mail_select import read_outreach
    from outreach_render_mail import MailStep
    from smart_mail.smart_campaign_sender import compute_send_budget, _backfill_mail_sequence

    db = _get_db()

    # Migrate old mail.subject/body campaigns so they appear in read_outreach()
    _backfill_mail_sequence(db)

    print("[dry-run] read_outreach  mode=%s  limit=%d" % (args.mode, args.limit))
    if args.campaign:
        print("[dry-run] filtering to campaign: %s" % args.campaign)

    batches = read_outreach(mode=args.mode, limit=args.limit)

    if not batches:
        print("No outreach candidates found.")
        return

    total_would_send  = 0
    total_would_skip  = 0
    total_no_password = 0
    total_campaigns   = 0

    for batch in batches:
        account = batch.account

        # Filter campaigns if --campaign given
        campaigns = [
            cwc for cwc in batch.campaigns
            if not args.campaign or cwc.campaign.campaign_id == args.campaign
        ]
        if not campaigns:
            continue

        n_contacts = sum(len(cwc.contacts) for cwc in campaigns)
        has_password = _password_available(account.email)
        budget = compute_send_budget(db, account.email) if has_password else 0

        pw_label = ("OK  env=%s" % _get_smtp_password_envvar(account.email)) if has_password \
                   else ("MISSING  env=%s not set" % _get_smtp_password_envvar(account.email))

        print(
            "\nAccount : %s  (%s)  host=%s:%d  %s"
            % (account.email, account.from_name, account.host, account.port,
               "SSL" if account.use_ssl else "STARTTLS")
        )
        print(
            "  password: %s  |  budget: %d  |  campaigns: %d  contacts: %d"
            % (pw_label, budget, len(campaigns), n_contacts)
        )

        if not has_password:
            total_no_password += n_contacts
            continue

        for cwc in campaigns:
            campaign = cwc.campaign
            total_campaigns += 1
            print(
                "\n  Campaign : %s  (%s)  seq_len=%d  contacts=%d"
                % (campaign.campaign_id, campaign.campaign_name,
                   len(campaign.mail_sequence), len(cwc.contacts))
            )

            for contact in cwc.contacts:
                mail_sent = (contact.extra or {}).get("mail_sent") or []
                next_idx  = len(mail_sent)

                if next_idx >= len(campaign.mail_sequence):
                    print(
                        "    %-35s  [SKIP -- sequence exhausted idx=%d]"
                        % (contact.email, next_idx)
                    )
                    total_would_skip += 1
                    continue

                step_dict = campaign.mail_sequence[next_idx]
                step = MailStep(
                    index     = step_dict.get("index",     next_idx),
                    mail_type = step_dict.get("mail_type", args.mode),
                    subject   = step_dict.get("subject",   ""),
                    body_html = (
                        step_dict.get("body_html", "")
                        or step_dict.get("body", "")
                    ),
                    body_text = step_dict.get("body_text", ""),
                )

                try:
                    _render_contact(step, contact, args.preview)
                    total_would_send += 1
                except Exception as ex:
                    print("    %-35s  [RENDER ERROR: %s]" % (contact.email, ex))
                    total_would_skip += 1

    print(
        "\n[dry-run] total  accounts=%d  campaigns=%d  "
        "would_send=%d  would_skip=%d  no_password=%d  "
        "(nothing sent, nothing written)"
        % (len(batches), total_campaigns,
           total_would_send, total_would_skip, total_no_password)
    )


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
        description="Dry-run the outreach send loop -- renders each mail without sending.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage examples")[-1] if "Usage examples" in __doc__ else "",
    )
    p.add_argument(
        "--mode", "-m",
        choices=["intro", "followup"],
        default="intro",
        help="intro = pending contacts (default); followup = followup_status=='Send mail'",
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
        "--preview",
        action="store_true",
        help="Show rendered subject and body snippet for each contact",
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

    _run(args)


if __name__ == "__main__":
    main()
