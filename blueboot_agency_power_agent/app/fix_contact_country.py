"""fix_contact_country.py — one-time migration to fix country fields on contact docs.

Problem: contacts were written with `country = lead.country_name` (e.g. "Norway")
instead of `country = lead.country` (ISO code "NO") + a separate `country_name` field.

This script:
  1. Loads all lead documents into memory  → {lead_id: {country, country_name}}
  2. Streams all contacts via collection_group("contacts")
  3. For each contact, looks up the parent lead and writes:
       country      = ISO code  (e.g. "NO")
       country_name = full name (e.g. "Norway")
  4. Batch-writes in groups of 400 ops; prints progress every 100 updates.

Usage:
    python app\fix_contact_country.py --dry-run    ← preview only
    python app\fix_contact_country.py              ← live run

Options:
    --collection NAME   Firestore leads collection (default: leads)
    --dry-run           Print what would be changed without writing
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import _pathsetup  # noqa: F401


# ---------------------------------------------------------------------------
# Firebase helpers
# ---------------------------------------------------------------------------

def _get_db(collection: str | None = None):
    try:
        import firebase_admin
        from firebase_admin import firestore
        import firebase_admin.credentials as fb_creds
    except ImportError:
        print("[fix] firebase-admin not installed — run: pip install firebase-admin")
        return None, None, None

    secrets_path = Path(__file__).parent.parent / "blueboot_secrets.py"
    key_dict = None
    if secrets_path.exists():
        try:
            spec = importlib.util.spec_from_file_location("blueboot_secrets", secrets_path)
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            key_dict = getattr(mod, "fireBaseAdminKey", None)
        except Exception as e:
            print(f"[fix] could not load blueboot_secrets: {e}")

    cred = (fb_creds.Certificate(key_dict) if key_dict
            else fb_creds.Certificate(os.getenv("FIREBASE_CREDENTIALS",
                                                "config/serviceAccountKey.json")))
    col_name = collection or os.getenv("FIRESTORE_COLLECTION", "leads")
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    db  = firestore.client()
    col = db.collection(col_name)
    return db, col, col_name


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def fix_contact_country(collection: str | None = None, dry_run: bool = False) -> None:
    db, col, col_name = _get_db(collection)
    if col is None:
        raise RuntimeError("Could not connect to Firestore.")

    tag = " [DRY RUN]" if dry_run else ""
    print(f"[fix] Collection : {col_name}{tag}")

    # ------------------------------------------------------------------
    # Step 1 — load all lead documents into memory
    # We need country (code) and country_name for every lead_id.
    # ------------------------------------------------------------------
    print("[fix] Loading leads…")
    leads_info: dict[str, dict] = {}   # lead_id → {country, country_name}
    for ldoc in col.stream():
        d = ldoc.to_dict() or {}
        lid = d.get("lead_id") or ldoc.id
        leads_info[lid] = {
            "country":      (d.get("country")      or "").strip(),
            "country_name": (d.get("country_name") or "").strip(),
        }
    print(f"[fix] {len(leads_info)} leads loaded.")

    # ------------------------------------------------------------------
    # Step 2 — stream all contacts and collect updates
    # ------------------------------------------------------------------
    print("[fix] Scanning contacts…")

    MAX_BATCH      = 400
    PROGRESS_EVERY = 100

    batch         = db.batch() if not dry_run else None
    ops           = 0
    scanned       = 0
    updated       = 0
    skipped_no_lead = 0
    skipped_already = 0

    def _flush():
        nonlocal batch, ops
        if ops and not dry_run:
            batch.commit()
        batch = db.batch() if not dry_run else None
        ops   = 0

    for cdoc in db.collection_group("contacts").stream():
        scanned += 1
        c   = cdoc.to_dict() or {}
        lid = (c.get("lead_id") or "").strip()

        if not lid or lid not in leads_info:
            skipped_no_lead += 1
            continue

        info         = leads_info[lid]
        new_country  = info["country"]
        new_country_name = info["country_name"]

        # Nothing to do if already correct
        if (c.get("country") == new_country
                and c.get("country_name") == new_country_name):
            skipped_already += 1
            continue

        old_country = c.get("country", "")

        if dry_run:
            updated += 1
            if updated <= 20 or updated % PROGRESS_EVERY == 0:
                print(f"  [{updated}] {c.get('email', '')} / {c.get('company', '')}")
                print(f"        country      : {old_country!r:20} → {new_country!r}")
                print(f"        country_name : {c.get('country_name', '')!r:20} → {new_country_name!r}")
        else:
            batch.update(cdoc.reference, {
                "country":      new_country,
                "country_name": new_country_name,
            })
            ops   += 1
            updated += 1
            if updated % PROGRESS_EVERY == 0:
                print(f"  [fix] {updated} contacts updated…")
            if ops >= MAX_BATCH:
                _flush()

    _flush()

    print(f"\n[fix] Done{tag}.")
    print(f"  Scanned         : {scanned}")
    print(f"  Updated         : {updated}")
    print(f"  Already correct : {skipped_already}")
    print(f"  No lead found   : {skipped_no_lead}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(
        description="One-time migration: fix country/country_name fields on contact docs."
    )
    p.add_argument("--collection", metavar="NAME", default=None,
                   help="Firestore leads collection (default: leads)")
    p.add_argument("--dry-run", action="store_true",
                   help="Preview changes without writing to Firestore")
    args = p.parse_args(argv)
    fix_contact_country(collection=args.collection, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
