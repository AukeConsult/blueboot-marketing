"""seed_batch_jobs.py — Write cloud_batch job definitions to Firestore.

The Cloud Run batch-runner only seeds a job doc if it does NOT already exist,
so user edits made via the Edit Job modal are never overwritten on restart.

Use this script to:
  • First-time setup before Cloud Run is deployed
  • Force-update all job schemas after editing the JSON files (use --force)

Usage:
    python app/seed_batch_jobs.py                # skip existing docs
    python app/seed_batch_jobs.py --force        # overwrite all (fixes stale params)
    python app/seed_batch_jobs.py --dry-run      # preview without writing
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
    parser.add_argument("--force",   action="store_true", help="Overwrite existing docs (reset to JSON schema)")
    args = parser.parse_args(argv)

    db    = get_firestore()
    files = sorted(DEFS_DIR.glob("*.json"))

    if not files:
        print(f"No JSON files found in {DEFS_DIR}")
        return

    mode = "DRY RUN" if args.dry_run else ("FORCE" if args.force else "MERGE")
    print(f"[{mode}] Seeding {len(files)} job definition(s) to Firestore/{COLLECTION}/\n")

    written = skipped = 0
    for f in files:
        defn = json.loads(f.read_text())
        name = defn.get("name", f.stem)
        ref  = db.collection(COLLECTION).document(name)

        exists = ref.get().exists

        if exists and not args.force:
            print(f"  {name}  (skipped -- already exists; use --force to overwrite)")
            skipped += 1
            continue

        action = "would write" if args.dry_run else ("overwrite" if exists else "create")
        print(f"  {name}  ({action})")

        if not args.dry_run:
            # --force: full set() resets to JSON schema (removes stale required flags etc.)
            # default: merge=True preserves extra top-level fields
            if args.force:
                ref.set(defn)
            else:
                ref.set(defn, merge=True)
            written += 1

    print(f"\nWritten: {written}  Skipped: {skipped}")
    if written:
        print("Refresh the Google Jobs page to see the updated schemas.")
    if args.force:
        print("Note: tasks/ subcollection is NOT affected -- only the top-level job doc was reset.")


if __name__ == "__main__":
    main()
