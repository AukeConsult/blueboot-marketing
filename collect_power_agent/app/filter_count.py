"""filter_count.py -- Run the filter-facets count job from the command line and
optionally verify it agrees with the copy-to-campaign job.

The count job and the copy-to-campaign job share one matcher per pipeline
(site_leads / leads) so the contacts & sites the counts section reports are
exactly what a campaign would be built from. This CLI lets you exercise that
on real Firestore data.

Usage:
    # run the count for a facet (auto-detects pipeline) and store it
    python app/filter_count.py --facet site_leads

    # compute counts but DON'T write them back to the facet doc
    python app/filter_count.py --facet NO_ecom --no-write

    # run the count AND the copy dry-run, then assert they match (no writes)
    python app/filter_count.py --facet NO_ecom --compare

Options:
    --facet     Name of the filter_facets document.
    --no-write  Compute the canonical matched set and print it without updating
                the facet doc (uses the shared matcher directly).
    --compare   Also run the copy-to-campaign dry-run and check that
                copied == counted (accounting for cross-campaign dedup).

Synchronous, single-threaded -- sync Firestore reads/writes, no asyncio.
"""
from __future__ import annotations

import argparse
import json
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


def _pipeline_of(db, facet_name: str) -> tuple[str, dict]:
    """Return (pipeline, filters) for the facet doc; raise if missing."""
    from crm.filter_count_lib import FILTER_FACETS_COLLECTION
    snap = db.collection(FILTER_FACETS_COLLECTION).document(facet_name).get()
    if not snap.exists:
        raise ValueError(f"filter_facets/'{facet_name}' not found")
    doc = snap.to_dict() or {}
    return doc.get("pipeline", "site_leads"), (doc.get("filters") or {})


def _run_count(db, facet_name: str, pipeline: str) -> dict:
    from crm.filter_count_lib import run_filter_count, run_leads_filter_count
    if pipeline == "leads":
        return run_leads_filter_count(db, facet_name)
    return run_filter_count(db, facet_name)


def _canonical_set(db, filters: dict, pipeline: str) -> tuple[set, set]:
    """(matched_email_ids, matched_sites/leads) without writing anything."""
    from crm.filter_count_lib import (
        site_leads_matched_email_ids, leads_matched_email_ids,
    )
    if pipeline == "leads":
        return leads_matched_email_ids(db, filters)
    return site_leads_matched_email_ids(db, filters)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="Run / verify the filter-facets count job.")
    ap.add_argument("--facet", required=True, help="filter_facets document name")
    ap.add_argument("--no-write", action="store_true",
                    help="compute the matched set without updating the facet doc")
    ap.add_argument("--compare", action="store_true",
                    help="also run the copy dry-run and assert copied == counted")
    args = ap.parse_args(argv)

    db = _get_db()
    pipeline, filters = _pipeline_of(db, args.facet)
    print(f"[filter-count] facet='{args.facet}'  pipeline='{pipeline}'", flush=True)

    # Canonical matched set (shared matcher, no writes)
    mids, msites = _canonical_set(db, filters, pipeline)
    site_label = "leads" if pipeline == "leads" else "sites"
    print(f"[filter-count] canonical matched set: "
          f"{len(mids)} contacts across {len(msites)} {site_label}", flush=True)

    if not args.no_write:
        counts = _run_count(db, args.facet, pipeline)
        print("\n[filter-count] stored counts:")
        print(json.dumps(counts, indent=2, ensure_ascii=False))
        # the canonical contacts must equal the stored copyable count
        stored = counts.get("contacts_in_email_contacts")
        if stored != len(mids):
            print(f"\n  WARNING: stored contacts_in_email_contacts={stored} != "
                  f"canonical {len(mids)} -- investigate.", file=sys.stderr)

    if args.compare:
        from crm.facet_campaign_lib import run_facet_campaign
        print("\n[filter-count] running copy-to-campaign DRY RUN for comparison…",
              flush=True)
        res = run_facet_campaign(db, args.facet, "filtercount_compare_tmp", dry_run=True)
        copied = res.get("contacts_matched", 0)
        deduped = res.get("contacts_skipped_dedup", 0)
        print(f"  copy dry-run: contacts_matched={copied}  "
              f"skipped_dedup={deduped}  sites_count={res.get('sites_count')}")
        # Rule 3: copied + cross-campaign dedup == canonical counted contacts.
        expected = len(mids)
        ok = (copied + deduped) == expected
        print(f"\n  CHECK  copied({copied}) + deduped({deduped}) "
              f"== counted({expected})  ->  {'OK' if ok else 'MISMATCH'}")
        if not ok:
            sys.exit(2)


if __name__ == "__main__":
    main()
