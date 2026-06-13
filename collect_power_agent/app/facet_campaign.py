"""facet_campaign.py -- Create a campaign from a saved filter-facets preset.

Reads the selected values from a filter_facets Firestore document, collects
matching email_contacts, deduplicates against contacts already assigned to
other campaigns, then creates (or updates) a campaign document and populates
its campaign_contacts subcollection.

Usage:
    python app/facet_campaign.py --facet site_leads --campaign NO_tech_jul01
    python app/facet_campaign.py --facet NO_ecom --campaign NO_ecom_jul01 --dry-run

Options:
    --facet     Name of the filter_facets document to use as the source filter.
    --campaign  Target campaign ID (created if not found; contacts refreshed if exists).
    --dry-run   Compute everything and print a summary without writing anything.

Synchronous, single-threaded — no asyncio needed (sync Firestore reads/writes).
"""
from __future__ import annotations

import argparse
import json
import sys

import _pathsetup  # noqa: F401  -- sets Windows selector loop / sys.path

# Make functions-crm/ importable as `crm.*` (same as the Cloud Run worker).
_CRM_DIR = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "functions-crm")
if _CRM_DIR not in __import__("sys").path:
    __import__("sys").path.insert(0, _CRM_DIR)


def _get_db():
    try:
        from app.firestore_client import get_firestore
    except ImportError:
        from firestore_client import get_firestore
    return get_firestore()


def _run(facet_name: str, campaign_id: str, dry_run: bool) -> dict:
    from crm.facet_campaign_lib import run_facet_campaign
    db = _get_db()
    return run_facet_campaign(
        db=db,
        facet_name=facet_name,
        campaign_id=campaign_id,
        dry_run=dry_run,
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Create a campaign from a saved filter-facets preset.")
    ap.add_argument(
        "--facet", required=True,
        help="filter_facets document name (e.g. 'site_leads' or a saved preset name)")
    ap.add_argument(
        "--campaign", required=True,
        help="Target campaign ID — created if absent, contacts refreshed if present")
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Compute and print what would be written without touching Firestore")
    args = ap.parse_args()

    print(f"[facet-campaign] facet='{args.facet}'  campaign='{args.campaign}'  "
          f"dry_run={args.dry_run}", flush=True)

    try:
        result = _run(
            facet_name=args.facet,
            campaign_id=args.campaign,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        print(f"[facet-campaign] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print("\n[facet-campaign] Result:")
    print(json.dumps(result, indent=2, ensure_ascii=False))

    blocked   = result.get("emails_in_other_campaigns", 0)
    overlaps  = result.get("contacts_skipped_dedup", 0)
    added     = result.get("contacts_added")
    refreshed = result.get("contacts_refreshed")
    removed   = result.get("contacts_removed")
    protected = result.get("contacts_protected")
    if result.get("dry_run"):
        print(f"\n  {result['contacts_matched']} contacts would be written to "
              f"campaign '{result['campaign_id']}' (add/refresh split unknown in dry-run).")
        print(f"  Dedup: {blocked} emails exist in other campaigns, "
              f"{overlaps} of those overlap with this filter's results.")
    else:
        print(f"\n  Campaign '{result['campaign_id']}': "
              f"+{added} new  ~{refreshed} refreshed  -{removed} removed  "
              f"{protected} protected (non-pending, no longer match).")
        print(f"  Dedup: {blocked} emails in other campaigns, "
              f"{overlaps} skipped (overlap with this filter).")


if __name__ == "__main__":
    main()
