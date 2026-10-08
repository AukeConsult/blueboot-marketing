"""prospects_import.py -- Sync the BlueSearch prospect catalogue into campaigns.

Reads every .xlsx in the prospects folder, groups rows by country and upserts
them into one campaign per country (BS_UK, BS_DK, ...).  New rows are created,
changed rows are updated field-by-field, unchanged rows are left alone.
Outreach state (status, mail_sent, follow-up...) is never touched.

DRY RUN BY DEFAULT -- nothing is written until you pass --apply.

Usage:
    python app/prospects_import.py                      # preview everything
    python app/prospects_import.py --apply              # write
    python app/prospects_import.py --country UK,DK      # only these countries
    python app/prospects_import.py --file BlueSearch-UK-prospects.xlsx
    python app/prospects_import.py --dir "D:\\copy\\prospects" --prefix BS
    python app/prospects_import.py --campaign-only      # skip site_leads/site_contacts/email_contacts
    python app/prospects_import.py --show-changes 50    # list more field-level changes
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import _pathsetup  # noqa: F401

_CRM_DIR = str(Path(__file__).resolve().parent.parent / "functions-crm")
if _CRM_DIR not in sys.path:
    sys.path.insert(0, _CRM_DIR)

DEFAULT_DIR = r"G:\Shared drives\BlueBoot R&D\marketing\prospects"


def _get_db():
    try:
        from app.firestore_client import get_firestore
    except ImportError:
        from firestore_client import get_firestore
    return get_firestore()


def main(argv=None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    p = argparse.ArgumentParser(
        description="Sync the prospect catalogue (xlsx) into campaigns. Dry run unless --apply.")
    p.add_argument("--dir", default=os.getenv("PROSPECTS_DIR", DEFAULT_DIR),
                   help="Folder with the prospect .xlsx files (env PROSPECTS_DIR)")
    p.add_argument("--file", action="append", default=[],
                   help="Only this file (name inside --dir, or full path). Repeatable.")
    p.add_argument("--prefix", default="BS", help="Campaign prefix -> <prefix>_<CC> (default BS)")
    p.add_argument("--campaign", default="",
                   help="Put ALL rows into this single campaign instead of one per country")
    p.add_argument("--country", default="",
                   help="Comma-separated country codes to include, e.g. UK,DK")
    p.add_argument("--campaign-only", action="store_true",
                   help="Write only campaign_leads/campaign_contacts "
                        "(skip site_leads, site_contacts, email_contacts)")
    p.add_argument("--show-changes", type=int, default=10, metavar="N",
                   help="Show up to N field-level changes per campaign (default 10)")
    p.add_argument("--apply", action="store_true", help="Write to Firestore (default: dry run)")
    args = p.parse_args(argv)

    from crm.prospect_import_lib import (
        parse_catalogue, build_records, build_plan, apply_plan, print_report)

    base = Path(args.dir)
    if args.file:
        paths = [Path(f) if Path(f).is_absolute() else base / f for f in args.file]
    else:
        paths = sorted(x for x in base.glob("*.xlsx") if not x.name.startswith("~$"))
    missing = [x for x in paths if not x.exists()]
    if missing or not paths:
        print(f"[prospects] ERROR: no files found ({', '.join(map(str, missing)) or base})",
              file=sys.stderr)
        return 1

    print(f"[prospects] {len(paths)} file(s) in {base}", flush=True)
    rows, warnings = parse_catalogue(paths)
    print(f"[prospects] {len(rows)} rows read", flush=True)

    countries = {c.strip().upper() for c in args.country.split(",") if c.strip()} or None
    records, w2 = build_records(rows, prefix=args.prefix,
                                only_campaign=args.campaign, countries=countries)
    warnings += w2
    if not records:
        print("[prospects] nothing to import", file=sys.stderr)
        return 1

    db = _get_db()
    plan = build_plan(db, records, campaign_only=args.campaign_only)
    print_report(plan, warnings, show_changes=args.show_changes)

    totals = {"new": 0, "changed": 0}
    for e in plan.values():
        for k in ("lead_stats", "contact_stats"):
            totals["new"] += e[k]["new"]
            totals["changed"] += e[k]["changed"]

    if not args.apply:
        print(f"DRY RUN -- {totals['new']} new and {totals['changed']} changed "
              f"campaign records. Re-run with --apply to write.")
        return 0

    written = apply_plan(db, plan, source_files=[x.name for x in paths])
    print("Done -- written:", ", ".join(f"{k}={v}" for k, v in written.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
