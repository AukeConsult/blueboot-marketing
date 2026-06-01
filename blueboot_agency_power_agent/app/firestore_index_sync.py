"""firestore_index_sync.py — Merge new Firestore indexes into firestore.indexes.json.

Reads the existing firestore.indexes.json (if present), merges in the NEW_INDEXES
defined below, de-duplicates, and writes the result back.

Also introspects Firestore via the Admin SDK to discover collections and
collectionGroups actually present, and prints a report.

Usage:
    python app/firestore_index_sync.py                  # merge + write
    python app/firestore_index_sync.py --dry-run        # print merged JSON, don't write
    python app/firestore_index_sync.py --discover-only  # just list collections
    python app/firestore_index_sync.py --output path/to/firestore.indexes.json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path

import _pathsetup  # noqa: F401

# ---------------------------------------------------------------------------
# New indexes to add
# ---------------------------------------------------------------------------

NEW_INDEXES = [
    # ── site_campaigns / site_leads ─────────────────────────────────────────
    {
        "collectionGroup": "site_leads",
        "queryScope": "COLLECTION_GROUP",
        "fields": [
            {"fieldPath": "ai_country", "order": "ASCENDING"},
            {"fieldPath": "crawled_at", "order": "DESCENDING"},
        ],
    },
    {
        "collectionGroup": "site_leads",
        "queryScope": "COLLECTION_GROUP",
        "fields": [
            {"fieldPath": "ai_country",  "order": "ASCENDING"},
            {"fieldPath": "ai_sector",   "order": "ASCENDING"},
            {"fieldPath": "crawled_at",  "order": "DESCENDING"},
        ],
    },
    {
        "collectionGroup": "site_leads",
        "queryScope": "COLLECTION_GROUP",
        "fields": [
            {"fieldPath": "ai_country",    "order": "ASCENDING"},
            {"fieldPath": "ai_confidence", "order": "DESCENDING"},
        ],
    },
    {
        "collectionGroup": "site_leads",
        "queryScope": "COLLECTION_GROUP",
        "fields": [
            {"fieldPath": "ai_country",      "order": "ASCENDING"},
            {"fieldPath": "ai_sector",       "order": "ASCENDING"},
            {"fieldPath": "ai_confidence",   "order": "DESCENDING"},
        ],
    },
    # ── site_campaigns / site_campaign_sites ────────────────────────────────
    {
        "collectionGroup": "site_campaign_sites",
        "queryScope": "COLLECTION_GROUP",
        "fields": [
            {"fieldPath": "ai_country", "order": "ASCENDING"},
            {"fieldPath": "crawled_at", "order": "DESCENDING"},
        ],
    },
    {
        "collectionGroup": "site_campaign_sites",
        "queryScope": "COLLECTION_GROUP",
        "fields": [
            {"fieldPath": "ai_country",  "order": "ASCENDING"},
            {"fieldPath": "ai_sector",   "order": "ASCENDING"},
            {"fieldPath": "ai_confidence", "order": "DESCENDING"},
        ],
    },
    # ── site_campaigns / site_campaign_sites / site_campaign_contacts ────────
    {
        "collectionGroup": "site_campaign_contacts",
        "queryScope": "COLLECTION_GROUP",
        "fields": [
            {"fieldPath": "ai_country", "order": "ASCENDING"},
            {"fieldPath": "name",       "order": "ASCENDING"},
        ],
    },
    {
        "collectionGroup": "site_campaign_contacts",
        "queryScope": "COLLECTION_GROUP",
        "fields": [
            {"fieldPath": "ai_country",        "order": "ASCENDING"},
            {"fieldPath": "email",             "order": "ASCENDING"},
            {"fieldPath": "brave_enriched_at", "order": "DESCENDING"},
        ],
    },
    {
        "collectionGroup": "site_campaign_contacts",
        "queryScope": "COLLECTION_GROUP",
        "fields": [
            {"fieldPath": "ai_country",  "order": "ASCENDING"},
            {"fieldPath": "occupation",  "order": "ASCENDING"},
            {"fieldPath": "name",        "order": "ASCENDING"},
        ],
    },
    # ── leads_extract / contacts_extracted ─────────────────────────────────────
    {
        "collectionGroup": "contacts_extracted",
        "queryScope": "COLLECTION_GROUP",
        "fields": [
            {"fieldPath": "extract_id", "order": "ASCENDING"},
            {"fieldPath": "country",    "order": "ASCENDING"},
        ],
    },
    {
        "collectionGroup": "contacts_extracted",
        "queryScope": "COLLECTION_GROUP",
        "fields": [
            {"fieldPath": "extract_id", "order": "ASCENDING"},
            {"fieldPath": "email",      "order": "ASCENDING"},
        ],
    },
    # ── site_leads / site_contacts (top-level collections) ───────────────────
    {
        "collectionGroup": "site_contacts",
        "queryScope": "COLLECTION_GROUP",
        "fields": [
            {"fieldPath": "ai_country", "order": "ASCENDING"},
            {"fieldPath": "name",       "order": "ASCENDING"},
        ],
    },
    {
        "collectionGroup": "site_contacts",
        "queryScope": "COLLECTION_GROUP",
        "fields": [
            {"fieldPath": "ai_country",         "order": "ASCENDING"},
            {"fieldPath": "email",              "order": "ASCENDING"},
            {"fieldPath": "brave_enriched_at",  "order": "DESCENDING"},
        ],
    },
    {
        "collectionGroup": "site_contacts",
        "queryScope": "COLLECTION_GROUP",
        "fields": [
            {"fieldPath": "ai_country",  "order": "ASCENDING"},
            {"fieldPath": "occupation",  "order": "ASCENDING"},
            {"fieldPath": "name",        "order": "ASCENDING"},
        ],
    },
    {
        "collectionGroup": "site_contacts",
        "queryScope": "COLLECTION_GROUP",
        "fields": [
            {"fieldPath": "ai_country",        "order": "ASCENDING"},
            {"fieldPath": "brave_enriched_at", "order": "DESCENDING"},
        ],
    },
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _index_key(idx: dict) -> str:
    """Stable string key for de-duplication."""
    fields_str = "|".join(
        f"{f['fieldPath']}:{f.get('order', f.get('arrayConfig', ''))}"
        for f in idx.get("fields", [])
    )
    return f"{idx.get('collectionGroup')}:{idx.get('queryScope')}:{fields_str}"


def _load_existing(path: Path) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            print(f"  [index-sync] Loaded existing: {path}  "
                  f"({len(data.get('indexes', []))} indexes, "
                  f"{len(data.get('fieldOverrides', []))} overrides)")
            return data
        except Exception as e:
            print(f"  [index-sync] Could not parse {path}: {e} — starting fresh")
    else:
        print(f"  [index-sync] No existing file at {path} — creating new")
    return {"indexes": [], "fieldOverrides": []}


def _merge(existing: dict, new_indexes: list[dict]) -> tuple[dict, int, int]:
    """Merge new_indexes into existing, return (merged, added, skipped)."""
    seen   = {_index_key(idx): True for idx in existing.get("indexes", [])}
    added  = 0
    skipped = 0
    merged = list(existing.get("indexes", []))

    for idx in new_indexes:
        key = _index_key(idx)
        if key in seen:
            skipped += 1
        else:
            merged.append(idx)
            seen[key] = True
            added += 1

    result = dict(existing)
    result["indexes"] = merged
    return result, added, skipped


# ---------------------------------------------------------------------------
# Firestore discovery
# ---------------------------------------------------------------------------

def _load_secrets():
    secrets_path = Path(__file__).parent.parent / "blueboot_secrets.py"
    if not secrets_path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("blueboot_secrets", secrets_path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, "fireBaseAdminKey", None)
    except Exception as e:
        print(f"  [index-sync] could not load blueboot_secrets: {e}")
        return None


def _init_firestore(fb_key_dict):
    try:
        import firebase_admin
        from firebase_admin import firestore
        import firebase_admin.credentials as fb_creds
    except ImportError:
        raise RuntimeError("firebase-admin not installed — run: pip install firebase-admin")
    cred = (fb_creds.Certificate(fb_key_dict) if fb_key_dict
            else fb_creds.Certificate(os.getenv("FIREBASE_CREDENTIALS",
                                                "config/serviceAccountKey.json")))
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    return firestore.client()


def discover_collections(db) -> dict[str, list[str]]:
    """Return {collection_id: [sample_doc_ids]} for top-level collections."""
    print("\n  [index-sync] Discovering Firestore collections…")
    result: dict[str, list[str]] = {}
    for col in db.collections():
        sample = [doc.id for doc in col.limit(3).stream()]
        result[col.id] = sample
        print(f"    {col.id:<40} ({len(sample)} sample docs)")
    return result


def discover_subcollections(db, top_collections: list[str]) -> dict[str, list[str]]:
    """Sample first doc of each top-level collection and list its subcollections."""
    print("\n  [index-sync] Discovering subcollections…")
    found: dict[str, list[str]] = {}
    for col_id in top_collections:
        try:
            docs = list(db.collection(col_id).limit(1).stream())
            if not docs:
                continue
            doc_ref = docs[0].reference
            subcols = list(doc_ref.collections())
            if subcols:
                names = [s.id for s in subcols]
                found[col_id] = names
                for name in names:
                    print(f"    {col_id}/{docs[0].id}/{name}")
        except Exception as exc:
            print(f"    [skip] {col_id}: {exc}")
    return found


# ---------------------------------------------------------------------------
# Fetch live indexes from Firestore via firebase CLI
# ---------------------------------------------------------------------------

def _fetch_live_indexes(fb_key_dict: dict | None) -> list[dict]:
    """Fetch all deployed composite indexes via the Firestore Admin REST API.

    Uses the same service-account credentials as the rest of the project —
    no firebase CLI or Node.js required.
    Returns a list of index dicts compatible with firestore.indexes.json format.
    """
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
    except ImportError:
        print("  [index-sync] google-auth not installed — run: pip install google-auth")
        return []

    print("  [index-sync] Fetching live indexes via Firestore Admin REST API…")

    try:
        if fb_key_dict:
            creds = service_account.Credentials.from_service_account_info(
                fb_key_dict,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            project_id = fb_key_dict.get("project_id", "")
        else:
            import json as _json
            key_path = os.getenv("FIREBASE_CREDENTIALS", "config/serviceAccountKey.json")
            key_data = _json.loads(Path(key_path).read_text())
            creds = service_account.Credentials.from_service_account_info(
                key_data,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            project_id = key_data.get("project_id", "")

        if not project_id:
            print("  [index-sync] Could not determine project_id from credentials")
            return []

        session  = AuthorizedSession(creds)
        base     = (f"https://firestore.googleapis.com/v1/projects/{project_id}"
                    f"/databases/(default)")

        # ── 1. List all collectionGroups ─────────────────────────────────────
        cg_url   = f"{base}/collectionGroups/-/fields"
        cg_names: set[str] = set()
        cg_token = None
        try:
            while True:
                params = {"pageSize": 200}
                if cg_token:
                    params["pageToken"] = cg_token
                cg_resp = session.get(cg_url, params=params, timeout=30)
                if cg_resp.ok:
                    cg_data = cg_resp.json()
                    for f in cg_data.get("fields", []):
                        name = f.get("name", "")
                        # name: .../collectionGroups/{cg}/fields/{field}
                        if "/collectionGroups/" in name:
                            cg = name.split("/collectionGroups/")[1].split("/")[0]
                            if cg != "__default__":
                                cg_names.add(cg)
                    cg_token = cg_data.get("nextPageToken")
                    if not cg_token:
                        break
                else:
                    break
        except Exception:
            pass  # non-fatal — index fetch is the important part

        if cg_names:
            print(f"  [index-sync] CollectionGroups found: {', '.join(sorted(cg_names))}")

        # ── 2. Fetch all composite indexes (paginated) ───────────────────────
        idx_url    = f"{base}/collectionGroups/-/indexes"
        raw_indexes: list[dict] = []
        page_token = None
        while True:
            params = {"pageSize": 200}
            if page_token:
                params["pageToken"] = page_token
            resp = session.get(idx_url, params=params, timeout=30)
            resp.raise_for_status()
            page = resp.json()
            raw_indexes.extend(page.get("indexes", []))
            page_token = page.get("nextPageToken")
            if not page_token:
                break

    except Exception as exc:
        print(f"  [index-sync] REST API fetch failed: {exc}")
        return []
    clean: list[dict] = []
    skipped_states: list[str] = []
    for idx in raw_indexes:
        state = idx.get("state", "READY")
        if state not in ("READY", ""):
            skipped_states.append(state)
            continue
        cg = idx.get("name", "").rsplit("/collectionGroups/", 1)[-1].rsplit("/indexes/", 1)[0]
        entry = {
            "collectionGroup": cg,
            "queryScope":      idx.get("queryScope", "COLLECTION"),
            "fields": [
                {k: v for k, v in f.items() if k in ("fieldPath", "order", "arrayConfig")}
                for f in idx.get("fields", [])
                if f.get("fieldPath") != "__name__"   # skip internal __name__ field
            ],
        }
        if entry["fields"]:
            clean.append(entry)

    if skipped_states:
        from collections import Counter
        counts = Counter(skipped_states)
        print(f"  [index-sync] Skipped (not READY): { {k: v for k, v in counts.items()} }")

    print(f"  [index-sync] {len(clean)} live indexes fetched from Firestore")
    return clean


def _deploy_indexes(indexes_path: Path, fb_key_dict: dict | None) -> None:
    """Deploy indexes from firestore.indexes.json via the Firestore Admin REST API.

    Creates any index not yet present in Firestore.
    Skips indexes that already exist (same collectionGroup + fields).
    Does NOT delete indexes — only adds.
    """
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
    except ImportError:
        print("  [index-sync] google-auth not installed — run: pip install google-auth")
        return

    print("  [index-sync] Deploying indexes via Firestore Admin REST API…")

    try:
        data = json.loads(indexes_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [index-sync] Could not read {indexes_path}: {e}")
        return

    try:
        if fb_key_dict:
            creds = service_account.Credentials.from_service_account_info(
                fb_key_dict,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            project_id = fb_key_dict.get("project_id", "")
        else:
            import json as _json
            key_path = os.getenv("FIREBASE_CREDENTIALS", "config/serviceAccountKey.json")
            key_data = _json.loads(Path(key_path).read_text())
            creds = service_account.Credentials.from_service_account_info(
                key_data,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            project_id = key_data.get("project_id", "")
    except Exception as exc:
        print(f"  [index-sync] Credentials error: {exc}")
        return

    session   = AuthorizedSession(creds)
    base_url  = f"https://firestore.googleapis.com/v1/projects/{project_id}/databases/(default)"
    created = skipped = failed = 0

    for idx in data.get("indexes", []):
        cg     = idx["collectionGroup"]
        url    = f"{base_url}/collectionGroups/{cg}/indexes"
        payload = {
            "queryScope": idx.get("queryScope", "COLLECTION_GROUP"),
            "fields":     idx.get("fields", []),
        }
        try:
            resp = session.post(url, json=payload, timeout=30)
            if resp.status_code in (200, 201):
                created += 1
                print(f"    + created: {cg} {[f['fieldPath'] for f in idx['fields']]}")
            elif resp.status_code == 409:
                skipped += 1  # already exists
            else:
                failed += 1
                print(f"    ! failed ({resp.status_code}): {cg} — {resp.text[:120]}")
        except Exception as exc:
            failed += 1
            print(f"    ! error: {cg}: {exc}")

    print(f"  [index-sync] Deploy done — created: {created}  already existed: {skipped}  failed: {failed}")

# ---------------------------------------------------------------------------
# Summary printer
# ---------------------------------------------------------------------------

def _print_summary(merged: dict) -> None:
    """Print a grouped summary of all indexes in the merged file."""
    indexes = merged.get("indexes", [])
    overrides = merged.get("fieldOverrides", [])

    print("\n══════════════════════════════════════════════════════════════════")
    print(f"  firestore.indexes.json — {len(indexes)} composite indexes")
    print("══════════════════════════════════════════════════════════════════")

    # Group by collectionGroup + queryScope
    from collections import defaultdict
    groups: dict[str, list] = defaultdict(list)
    for idx in indexes:
        key = f"{idx.get('collectionGroup')}  [{idx.get('queryScope', '')}]"
        groups[key].append(idx)

    for group_key in sorted(groups):
        print(f"\n  {group_key}")
        for idx in groups[group_key]:
            fields_str = "  +  ".join(
                f"{f['fieldPath']} {'↑' if f.get('order','') == 'ASCENDING' else '↓' if f.get('order','') == 'DESCENDING' else f.get('arrayConfig','?')}"
                for f in idx.get("fields", [])
            )
            print(f"    · {fields_str}")

    if overrides:
        print(f"\n  fieldOverrides: {len(overrides)}")
        for ov in overrides:
            print(f"    · {ov.get('collectionGroup')} / {ov.get('fieldPath')}")

    print("══════════════════════════════════════════════════════════════════\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv=None) -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    p = argparse.ArgumentParser(
        description="Merge new Firestore indexes into firestore.indexes.json"
    )
    p.add_argument("--output",        default=None, metavar="FILE",
                   help="Path to firestore.indexes.json  "
                        "(default: project root / firestore.indexes.json)")
    p.add_argument("--dry-run",       action="store_true",
                   help="Print merged JSON without writing the file")
    p.add_argument("--discover-only", action="store_true",
                   help="List collections/subcollections, merge + write index file, then exit")
    p.add_argument("--no-discover",   action="store_true",
                   help="Skip collection/subcollection browsing — live index fetch still runs")
    p.add_argument("--deploy",        action="store_true",
                   help="Run 'firebase deploy --only firestore:indexes' after writing")
    args = p.parse_args(argv)

    output_path = Path(args.output) if args.output else (
        Path(__file__).parent.parent / "firestore.indexes.json"
    )

    # ── Firestore collection discovery (Admin SDK) ──────────────────────────
    _fb_key_for_discover = None
    if not args.no_discover:
        try:
            _fb_key_for_discover = _load_secrets()
            db  = _init_firestore(_fb_key_for_discover)
            top = discover_collections(db)
            discover_subcollections(db, list(top.keys()))
        except Exception as exc:
            print(f"\n  [index-sync] Discovery failed: {exc}")

    # ── Fetch live indexes via REST API (always — protects against deletions) ──
    # This runs even with --no-discover so deployed indexes are never dropped.
    print(f"\n  [index-sync] Merging indexes into {output_path.name}…")
    existing     = _load_existing(output_path)
    fb_key       = _fb_key_for_discover or _load_secrets()
    live_indexes = _fetch_live_indexes(fb_key)

    # Merge order: existing file → live Firestore → NEW_INDEXES
    # Each pass only adds what isn't already present
    step1, added1, _ = _merge(existing,        live_indexes)
    step2, added2, _ = _merge(step1,           NEW_INDEXES)
    merged           = step2
    total_added      = added1 + added2
    total_present    = len(merged["indexes"]) - total_added

    merged_json = json.dumps(merged, indent=2, ensure_ascii=False)

    from_file     = len(existing.get("indexes", []))
    from_firestore = added1
    from_new       = added2
    print(f"  [index-sync] From file: {from_file}  "
          f"From Firestore (new): {from_firestore}  "
          f"From NEW_INDEXES (new): {from_new}  "
          f"Total: {len(merged['indexes'])} indexes")

    if args.dry_run:
        print("\n── firestore.indexes.json (dry-run — not written) ─────────────")
        print(merged_json)
        _print_summary(merged)
        return

    # Write file
    output_path.write_text(merged_json + "\n", encoding="utf-8")
    print(f"  [index-sync] Written → {output_path}")

    # Always print summary
    _print_summary(merged)

    if args.discover_only:
        return

    # ── Deploy ───────────────────────────────────────────────────────────────
    if args.deploy:
        _deploy_indexes(output_path, fb_key)
    else:
        print("  Deploy with:  firebase deploy --only firestore:indexes")


if __name__ == "__main__":
    main()
