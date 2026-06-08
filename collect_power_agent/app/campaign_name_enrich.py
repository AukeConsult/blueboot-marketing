"""campaign_name_enrich.py -- CLI wrapper for contact name enrichment.

Core logic lives in functions-crm/crm/name_enrich_lib.py (the canonical version
deployed to Cloud Run). This script is the command-line entry point only.

Usage:
    python app/campaign_name_enrich.py --campaign MY_CAMPAIGN_ID
    python app/campaign_name_enrich.py --campaign MY_CAMPAIGN_ID --dry-run
    python app/campaign_name_enrich.py --campaign MY_CAMPAIGN_ID --skip-ai
    python app/campaign_name_enrich.py --campaign MY_CAMPAIGN_ID --debug
    python app/campaign_name_enrich.py --all          # all campaigns
    python app/campaign_name_enrich.py --all --dry-run --skip-ai
    python app/campaign_name_enrich.py --emails a@b.com c@d.com
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import _pathsetup  # noqa: F401  -- sets Windows selector loop / sys.path

# Make functions-crm/ importable as `crm.*` (same as the Cloud Run worker).
_CRM_DIR = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "functions-crm")
if _CRM_DIR not in sys.path:
    sys.path.insert(0, _CRM_DIR)


def _get_db():
    try:
        from app.firestore_client import get_firestore
    except ImportError:
        from firestore_client import get_firestore
    return get_firestore()


def main() -> None:
    from crm.name_enrich_lib import (
        _enrich, enrich_email_list, _doc_id_from_email,
    )

    ap = argparse.ArgumentParser(
        description="Fill missing contact names using rules + Bing + Brave + AI.")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--campaign", metavar="ID",
                     help="Campaign ID — enrich campaign_contacts for this campaign")
    grp.add_argument("--all", action="store_true",
                     help="Enrich ALL campaigns in the campaigns collection")
    grp.add_argument("--emails", metavar="EMAIL", nargs="+",
                     help="One or more email addresses to enrich directly")
    ap.add_argument("--dry-run",  action="store_true", help="Preview without writing")
    ap.add_argument("--skip-ai",  action="store_true", help="Rule-based only, no OpenAI")
    ap.add_argument("--model",    default="gpt-4o-mini", help="OpenAI model (default: gpt-4o-mini)")
    ap.add_argument("--batch",    type=int, default=5,   help="AI batch size (default: 5)")
    ap.add_argument("--limit",    type=int, default=None, help="Max contacts to process")
    ap.add_argument("--debug",    action="store_true",
                     help="Print Bing/Brave->AI payload and AI response; prepends leif@auke.no as calibration")
    args = ap.parse_args()

    db = _get_db()
    contacts: list[dict] = []

    if args.emails:
        print(f"[name-enrich] enriching {len(args.emails)} email(s) from --emails list...", flush=True)
        result = enrich_email_list(
            args.emails, db=db,
            dry_run=args.dry_run, skip_ai=args.skip_ai,
            model=args.model, batch_size=args.batch,
        )
        _print_summary(result, args.dry_run)
        return

    if args.campaign:
        print(f"[name-enrich] loading campaign_contacts for '{args.campaign}'...", flush=True)
        camp_ref = db.collection("campaigns").document(args.campaign)
        if not camp_ref.get().exists:
            print(f"[name-enrich] ERROR: campaign '{args.campaign}' not found", file=sys.stderr)
            sys.exit(1)
        for doc in camp_ref.collection("campaign_contacts").stream():
            data = doc.to_dict() or {}
            if data.get("name", "").strip():
                continue
            email = (data.get("email") or "").strip()
            if not email:
                continue
            contacts.append({
                "doc_id":       doc.id,
                "email":        email,
                "domain":       email.split("@")[1] if "@" in email else "",
                "campaign_ref": doc.reference,
                "ec_doc_id":    _doc_id_from_email(email),
            })
    else:
        print("[name-enrich] loading all campaigns...", flush=True)
        for camp_doc in db.collection("campaigns").stream():
            camp_id = camp_doc.id
            for doc in (db.collection("campaigns").document(camp_id)
                          .collection("campaign_contacts").stream()):
                data = doc.to_dict() or {}
                if data.get("name", "").strip():
                    continue
                email = (data.get("email") or "").strip()
                if not email:
                    continue
                contacts.append({
                    "doc_id":       doc.id,
                    "email":        email,
                    "domain":       email.split("@")[1] if "@" in email else "",
                    "campaign_ref": doc.reference,
                    "ec_doc_id":    _doc_id_from_email(email),
                    "_campaign_id": camp_id,
                })
        print(f"[name-enrich] found {len(contacts)} contacts without names across all campaigns",
              flush=True)

    if args.limit:
        contacts = contacts[:args.limit]
        print(f"[name-enrich] limited to {len(contacts)} contacts (--limit {args.limit})", flush=True)

    print(f"[name-enrich] {len(contacts)} contacts with missing names", flush=True)
    if not contacts:
        print("[name-enrich] Nothing to do.", flush=True)
        return

    result = asyncio.run(_enrich(
        db, contacts,
        dry_run=args.dry_run,
        skip_ai=args.skip_ai,
        model=args.model,
        batch_size=args.batch,
        skip_ec_lookup=False,
        debug=args.debug,
    ))
    _print_summary(result, args.dry_run)


def _print_summary(result: dict, dry_run: bool) -> None:
    print(f"\n[name-enrich] Done -- "
          f"rule={result['rule_resolved']}  ai={result['ai_resolved']}  "
          f"ec={result['ec_resolved']}  skipped={result['skipped']}  "
          f"written={result['written']}"
          f"{'  (DRY RUN)' if dry_run else ''}", flush=True)


if __name__ == "__main__":
    main()
