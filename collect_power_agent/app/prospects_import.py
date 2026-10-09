"""prospects_import.py -- Sync the BlueSearch prospect catalogue into campaigns.

Reads every .xlsx in the prospects folder, groups rows by country and upserts
them into one campaign per country (BS_UK, BS_DK, ...).  New rows are created,
changed rows are updated field-by-field, unchanged rows are left alone.
Outreach state (status, mail_sent, follow-up...) is never touched.

DRY RUN BY DEFAULT -- nothing is written until you pass --apply.

Usage:
    python app/prospects_import.py --dir "<folder>"      # preview everything (source is required)
    python app/prospects_import.py --dir "<folder>" --apply   # write
    python app/prospects_import.py --country UK,DK      # only these countries
    python app/prospects_import.py --file BlueSearch-UK-prospects.xlsx
    python app/prospects_import.py --dir "D:\\copy\\prospects" --prefix BS
    python app/prospects_import.py --file "D:\\copy\\BlueSearch-UK-prospects.xlsx"   # full path, no --dir
    python app/prospects_import.py --campaign-only      # skip site_leads/site_contacts/email_contacts
    python app/prospects_import.py --show-changes 50    # list more field-level changes

Site size pre-step (sitemap page count):
    python app/prospects_import.py --measure            # read sitemaps for rows with empty
                                                        # page_count, preview what would be written
    python app/prospects_import.py --measure --apply    # + write page_count / sitemap_url to the
                                                        # sheet (keeps a .xlsx.bak) and import it
    python app/prospects_import.py --measure-only --apply   # only update the sheets
    python app/prospects_import.py --measure --force --apply  # re-measure and OVERWRITE existing
                                                        # page_count / sitemap_url (sheet + Firestore)
    Without --force, existing sheet cells and campaign_leads.page_count are only filled when empty.
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
    p.add_argument("--dir", default=os.getenv("PROSPECTS_DIR", ""),
                   help="Folder with the prospect .xlsx files. Required unless --file gives "
                        "full paths; may also come from env / .env PROSPECTS_DIR")
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
    p.add_argument("--measure", action="store_true",
                   help="Pre-step: read each site's sitemap (rows with empty page_count) and "
                        "store page_count + sitemap_url in the sheet before importing")
    p.add_argument("--measure-only", action="store_true",
                   help="Run only the --measure pre-step, no Firestore import")
    p.add_argument("--force", action="store_true",
                   help="Overwrite existing page_count / sitemap_url (re-measure, replace sheet "
                        "cells and campaign_leads values). Default: only fill empty ones")
    p.add_argument("--workers", type=int, default=8, help="Parallel sitemap reads (default 8)")
    p.add_argument("--skip-dup-check", action="store_true",
                   help="Allow --apply even when the duplicate check against other campaigns failed "
                        "(NOT recommended: may create contacts that already exist elsewhere)")
    p.add_argument("--apply", action="store_true", help="Write to Firestore (default: dry run)")
    args = p.parse_args(argv)

    from crm.prospect_import_lib import (
        parse_catalogue, build_records, build_plan, apply_plan, print_report)

    if not args.dir and not (args.file and all(Path(f).is_absolute() for f in args.file)):
        p.error('source catalogue not given: use --dir "<folder with the .xlsx files>" '
                '(or --file with full paths, or set PROSPECTS_DIR)')
    base = Path(args.dir) if args.dir else Path(".")
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

    if args.measure or args.measure_only:
        from crm import site_size_lib as sz
        from crm.prospect_import_lib import country_code
        sel = [r for r in rows
               if not countries or country_code(r.get("country", "")) in countries]
        todo = sz.sites_to_measure(sel, force=args.force)
        print(f"[measure] {len(todo)} site(s) to read "
              f"({'all, --force' if args.force else 'empty page_count only'})", flush=True)
        results = sz.measure_sites(todo, workers=args.workers)
        if not todo:
            print("[measure] nothing to measure -- every site already has a page_count "
                  "(use --force to re-measure)", flush=True)
        print(f"[measure] step 2: {'writing' if args.apply else 'previewing'} "
              f"page_count / sitemap_url in {len(paths)} sheet file(s)", flush=True)
        sz.write_back(paths, results, force=args.force, apply=args.apply)
        n = sz.apply_to_rows(rows, results, force=args.force)
        print(f"[measure] {n} value(s) carried into the import"
              f"{'' if args.measure_only else ' -- step 3: planning the import ...'}", flush=True)
        if args.measure_only:
            if not args.apply:
                print("DRY RUN -- sheets not changed. Re-run with --apply to write.")
            return 0
    records, w2 = build_records(rows, prefix=args.prefix,
                                only_campaign=args.campaign, countries=countries)
    warnings += w2
    if not records:
        print("[prospects] nothing to import", file=sys.stderr)
        return 1

    db = _get_db()
    plan = build_plan(db, records, campaign_only=args.campaign_only,
                      force_size=args.force)
    print_report(plan, warnings, show_changes=args.show_changes)

    totals = {"new": 0, "changed": 0}
    for e in plan.values():
        for k in ("lead_stats", "contact_stats"):
            totals["new"] += e[k]["new"]
            totals["changed"] += e[k]["changed"]

    failed = [c for c, e in plan.items() if e["dup_check_error"]]
    if failed and args.apply and not args.skip_dup_check:
        print(f"[prospects] ABORTED: the duplicate check against other campaigns failed for "
              f"{', '.join(failed)} (see report). Nothing was written. Fix it (usually a missing "
              f"Firestore collection-group index on campaign_contacts.doc_id, the error message "
              f"has a link) or re-run with --skip-dup-check.", file=sys.stderr)
        return 2

    if not args.apply:
        print(f"DRY RUN -- {totals['new']} new and {totals['changed']} changed "
              f"campaign records. Re-run with --apply to write.")
        return 0

    written = apply_plan(db, plan, source_files=[x.name for x in paths])
    print("Done -- written:", ", ".join(f"{k}={v}" for k, v in written.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
