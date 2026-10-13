"""campaign_importer.py -- Import leads + contacts from Excel into a campaign.

Reads the standard export format (Leads+Contacts tab) and writes to:
  campaigns/{id}/campaign_leads/{lead_id}
  campaigns/{id}/campaign_contacts/{doc_id}

ID derivation
-------------
  lead_id  : from 'Lead ID' column, or derived from 'Website' via lead_id_from_url()
  doc_id   : always from email -- e.g. adrian@blisynlig.no -> adrian_blisynlig_no

Usage:
    python app/campaign_importer.py NO_tech_jul01 output/NO_tech_jul01/campaign.xlsx
    python app/campaign_importer.py NO_tech_jul01 campaign.xlsx --dry-run
"""
from __future__ import annotations

import argparse
import json
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


def main(argv=None) -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    p = argparse.ArgumentParser(
        description="Import leads + contacts from Excel into a campaign")
    p.add_argument("campaign_id", metavar="CAMPAIGN_ID",
                   help="Campaign ID (e.g. NO_tech_jul01). Created if missing.")
    p.add_argument("file", metavar="FILE",
                   help="Path to .xlsx file with a 'Leads+Contacts' tab")
    p.add_argument("--dry-run", action="store_true",
                   help="Preview counts without writing to Firestore")
    args = p.parse_args(argv)

    xlsx_path = Path(args.file)
    if not xlsx_path.exists():
        print(f"[campaign-import] ERROR: file not found: {xlsx_path}", file=sys.stderr)
        sys.exit(1)
    if xlsx_path.suffix.lower() != ".xlsx":
        print("[campaign-import] ERROR: file must be .xlsx", file=sys.stderr)
        sys.exit(1)

    from crm.campaign_import_lib import parse_sheet, run_campaign_import

    file_bytes = xlsx_path.read_bytes()

    print(f"[campaign-import] parsing {xlsx_path.name} …", flush=True)
    try:
        rows, warnings = parse_sheet(file_bytes)
    except Exception as exc:
        print(f"[campaign-import] ERROR parsing file: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"[campaign-import] {len(rows)} rows parsed", flush=True)
    for w in warnings:
        print(f"[campaign-import]   WARN {w}", flush=True)

    db = _get_db()
    try:
        result = run_campaign_import(
            db, args.campaign_id, rows, dry_run=args.dry_run)
    except Exception as exc:
        print(f"[campaign-import] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print()
    print(json.dumps(result, indent=2))
    print()
    if args.dry_run:
        print(f"  DRY RUN — {result['leads_new']} leads new, "
              f"{result['leads_updated']} leads update, "
              f"{result['contacts_new']} contacts new, "
              f"{result['contacts_updated']} contacts update, "
              f"{result['skipped']} skipped.")
        print("  Re-run without --dry-run to write.")
    else:
        total = (result['leads_new'] + result['leads_updated'] +
                 result['contacts_new'] + result['contacts_updated'])
        print(f"  Done — {total} records written to campaign '{args.campaign_id}'.")


if __name__ == "__main__":
    main()
