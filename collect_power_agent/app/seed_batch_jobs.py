"""seed_batch_jobs.py — Write cloud_batch job definitions to Firestore.

This is normally done automatically when the Cloud Run batch-runner starts up.
Run this script manually if the Google Jobs page shows 'No job definitions found'
before Cloud Run is deployed.

Usage:
    python app/seed_batch_jobs.py
    python app/seed_batch_jobs.py --dry-run
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
from pathlib import Path

from app.firestore_client import get_firestore

COLLECTION   = "gcloud-batch-jobs"
DEFS_DIR     = Path(__file__).parent.parent / "cloud_batch" / "job_definitions"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Seed Firestore with batch job definitions")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be written without writing")
    args = parser.parse_args(argv)

    db    = get_firestore()
    files = sorted(DEFS_DIR.glob("*.json"))

    if not files:
        print(f"No JSON files found in {DEFS_DIR}")
        return

    print(f"{'DRY RUN — ' if args.dry_run else ''}Seeding {len(files)} job definition(s) to Firestore/{COLLECTION}/\n")

    for f in files:
        defn = json.loads(f.read_text())
        name = defn.get("name", f.stem)
        print(f"  {name}")
        if not args.dry_run:
            db.collection(COLLECTION).document(name).set(defn, merge=True)

    print(f"\n{'Would write' if args.dry_run else 'Written'} {len(files)} definitions.")
    print("Refresh the Google Jobs page to see them.")


if __name__ == "__main__":
    main()
