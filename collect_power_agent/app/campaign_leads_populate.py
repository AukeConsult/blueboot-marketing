"""campaign_leads_populate.py -- Populate campaign_leads from existing campaign_contacts.

Reads the campaign_contacts already in a campaign, looks up each unique lead_id in
site_leads and leads, merges the data into a unified lead document, and writes it to
campaigns/{campaign_id}/campaign_leads/{lead_id}.

Usage:
    python app/campaign_leads_populate.py --campaign NO_tech_jul01
    python app/campaign_leads_populate.py --campaign NO_tech_jul01 --dry-run
    python app/campaign_leads_populate.py            # runs on ALL campaigns

Options:
    --campaign  Campaign ID to populate. Omit to run on all campaigns.
    --dry-run   Print what would be written without touching Firestore.

Safe to re-run: existing lead docs are updated with merge=True so any outreach state
is preserved. New leads get status="pending"; existing leads keep their current status.
"""
from __future__ import annotations

import argparse
import json
import sys

import _pathsetup  # noqa: F401  -- sets Windows selector loop + sys.path

_CRM_DIR = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "functions-crm")
if _CRM_DIR not in __import__("sys").path:
    __import__("sys").path.insert(0, _CRM_DIR)


def _get_db():
    try:
        from app.firestore_client import get_firestore
    except ImportError:
        from firestore_client import get_firestore
    return get_firestore()


def _all_campaign_ids(db) -> list[str]:
    from google.cloud.firestore_v1.base_query import FieldFilter
    docs = (
        db.collection("campaigns")
          .where(filter=FieldFilter("status", "!=", "deleting"))
          .select(["campaign_id"])
          .stream()
    )
    ids = []
    for doc in docs:
        cid = (doc.to_dict() or {}).get("campaign_id", "").strip() or doc.id
        if cid:
            ids.append(cid)
    return sorted(ids)


def _print_summary(result: dict) -> None:
    if result.get("dry_run"):
        print(f"  DRY RUN — {result['leads_found']} leads found across "
              f"{result['contacts_read']} contacts, "
              f"{result['leads_skipped']} would be skipped.")
    else:
        print(f"  {result['leads_written']} leads written, "
              f"{result['leads_skipped']} skipped.")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="Populate campaign_leads from existing campaign_contacts.")
    ap.add_argument(
        "--campaign", default=None,
        help="Campaign ID to populate (e.g. 'NO_tech_jul01'). Omit to run on all campaigns.")
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be written without touching Firestore")
    args = ap.parse_args(argv)

    from crm.campaign_leads_lib import populate_campaign_leads
    db = _get_db()

    # ── Single campaign ───────────────────────────────────────────────────────
    if args.campaign:
        try:
            result = populate_campaign_leads(
                db=db,
                campaign_id=args.campaign,
                dry_run=args.dry_run,
            )
        except Exception as exc:
            print(f"[campaign-leads] ERROR: {exc}", file=sys.stderr)
            sys.exit(1)

        print("\n[campaign-leads] Result:")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print()
        _print_summary(result)
        return

    # ── All campaigns ─────────────────────────────────────────────────────────
    campaign_ids = _all_campaign_ids(db)
    if not campaign_ids:
        print("[campaign-leads] No campaigns found.", file=sys.stderr)
        sys.exit(1)

    print(f"[campaign-leads] Running on {len(campaign_ids)} campaigns"
          f"{' (dry-run)' if args.dry_run else ''}:", flush=True)
    for cid in campaign_ids:
        print(f"  {cid}", flush=True)
    print()

    totals = {"contacts_read": 0, "leads_found": 0, "leads_written": 0, "leads_skipped": 0}
    errors = []

    for cid in campaign_ids:
        print(f"── {cid} ──", flush=True)
        try:
            result = populate_campaign_leads(db=db, campaign_id=cid, dry_run=args.dry_run)
        except Exception as exc:
            print(f"[campaign-leads] ERROR on '{cid}': {exc}", file=sys.stderr)
            errors.append(cid)
            continue

        _print_summary(result)
        for key in totals:
            totals[key] += result.get(key, 0)

    print(f"\n[campaign-leads] All done — {len(campaign_ids)} campaigns"
          f", {len(errors)} errors.")
    print(json.dumps(totals, indent=2))

    if errors:
        print(f"\nFailed campaigns: {errors}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
