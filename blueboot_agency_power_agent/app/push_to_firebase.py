"""Standalone script — push existing leads CSV to Firestore.

Usage:
  python push_to_firebase.py                  # reads output/agency_leads.csv
  python push_to_firebase.py --output mydir   # reads mydir/agency_leads.csv
  python push_to_firebase.py --collection agencies
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Push agency_leads.csv to Firestore"
    )
    parser.add_argument("--output",     default="output",
                        help="Directory containing agency_leads.csv (default: output)")
    parser.add_argument("--collection", default=None,
                        help="Firestore collection name (default: leads or FIRESTORE_COLLECTION env var)")
    args = parser.parse_args()

    if args.collection:
        os.environ["FIRESTORE_COLLECTION"] = args.collection

    csv_path = Path(args.output) / "agency_leads.csv"
    if not csv_path.exists():
        print(f"No CSV found at {csv_path}")
        sys.exit(1)

    from app.functions.models import load_existing_leads
    from app.functions.firebase_sync import sync_leads

    leads = load_existing_leads(Path(args.output))
    if not leads:
        print("No leads found in CSV — nothing to sync.")
        sys.exit(0)

    sync_leads(leads)


if __name__ == "__main__":
    main()
