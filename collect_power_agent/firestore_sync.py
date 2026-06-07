"""firestore_sync.py — Sync live Firestore indexes into firestore.indexes.json.

Reads the existing firestore.indexes.json (if present), fetches live indexes from
Firestore via the Admin REST API, merges them, de-duplicates, and writes the result back.

Run from the project root — used as a predeploy hook in firebase.json.

Also introspects Firestore via the Admin SDK to discover collections and
collectionGroups actually present, and prints a report.

Usage:
    python firestore_sync.py                  # merge + write
    python firestore_sync.py --dry-run        # print merged JSON, don't write
    python firestore_sync.py --discover-only  # just list collections
    python firestore_sync.py --output path/to/firestore.indexes.json
"""
from __future__ import annotations

import threading as _threading

# Guards firebase_admin.initialize_app against concurrent init
_local_fb_lock = _threading.Lock()
import argparse
import json
import os
from pathlib import Path

# Bootstrap: add app/ to sys.path so _pathsetup and internal modules resolve
# when this script is run from the project root.
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))
import _pathsetup  # noqa: F401

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

def _load_key_dict() -> dict:
    """Return the raw service-account JSON dict (needed for google-auth REST calls)."""
    from dotenv import load_dotenv
    load_dotenv()
    key_json = os.getenv("FIREBASE_KEY_JSON", "").strip()
    if key_json:
        return json.loads(key_json)
    creds_path = os.getenv("FIREBASE_CREDENTIALS", "") or str(
        Path(__file__).resolve().parent / "config" / "serviceAccountKey.json"
    )
    return json.loads(Path(creds_path).read_text(encoding="utf-8"))


def _load_secrets():
    """Return a firebase_admin Certificate (for Admin SDK / Firestore client)."""
    from dotenv import load_dotenv
    load_dotenv()
    from functions.firebase_cred import get_firebase_cred
    return get_firebase_cred()


def _init_firestore(fb_key_dict):
    try:
        import firebase_admin
        from firebase_admin import firestore
        import firebase_admin.credentials as fb_creds
    except ImportError:
        raise RuntimeError("firebase-admin not installed — run: pip install firebase-admin")
    if isinstance(fb_key_dict, fb_creds.Certificate):
        cred = fb_key_dict
    elif fb_key_dict:
        cred = (fb_key_dict if isinstance(fb_key_dict, fb_creds.Base) else fb_creds.Certificate(fb_key_dict))
    else:
        cred = fb_creds.Certificate(os.getenv("FIREBASE_CREDENTIALS",
                                              "config/serviceAccountKey.json"))
    with _local_fb_lock:
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

def _fetch_live_indexes(key_dict: dict, hint_cgs: set | None = None) -> list[dict]:
    """Fetch all deployed composite indexes via the Firestore Admin REST API.

    The wildcard collectionGroups/- endpoint is unreliable, so collection groups
    are discovered two ways:
      1. From the /fields endpoint (finds CGs with custom field overrides).
      2. From hint_cgs — CG names already present in the local index file
         (covers CGs that have composite indexes but no field overrides).
    Both sets are unioned before querying indexes per CG.
    """
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
    except ImportError as _e:
        raise RuntimeError("google-auth not installed — run: pip install google-auth requests") from _e

    print("  [index-sync] Fetching live indexes via Firestore Admin REST API…")

    try:
        creds = service_account.Credentials.from_service_account_info(
            key_dict,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        project_id = key_dict.get("project_id", "")
        if not project_id:
            raise RuntimeError("Could not determine project_id from credentials")

        session = AuthorizedSession(creds)
        base    = (f"https://firestore.googleapis.com/v1beta2/projects/{project_id}"
                   f"/databases/(default)")

        # ── 1. Collect collection group names via the working /fields endpoint ─
        fields_resp = session.get(
            f"{base}/collectionGroups/-/fields",
            params={"pageSize": 0, "filter": "indexConfig.usesAncestorConfig:false"},
            timeout=60,
        )
        if not fields_resp.ok:
            raise RuntimeError(
                f"Could not list collection groups: "
                f"{fields_resp.status_code} — {fields_resp.text[:300]}"
            )
        cg_names: set[str] = set()
        for f in fields_resp.json().get("fields", []):
            name = f.get("name", "")
            if "/collectionGroups/" in name:
                cg = name.split("/collectionGroups/")[1].split("/")[0]
                if cg not in ("__default__", "*"):
                    cg_names.add(cg)
        if hint_cgs:
            before = len(cg_names)
            cg_names |= hint_cgs
            extra = len(cg_names) - before
            if extra:
                print(f"  [index-sync] +{extra} CG(s) from existing index file")
        print(f"  [index-sync] Collection groups to query: {', '.join(sorted(cg_names)) or '(none)'}")

        # ── 2. Fetch composite indexes per collection group ────────────────────
        raw_indexes: list[dict] = []
        for cg in sorted(cg_names):
            url  = f"{base}/collectionGroups/{cg}/indexes"
            resp = session.get(url, timeout=30)
            if not resp.ok:
                print(f"  [index-sync] Warning: could not fetch indexes for {cg}: "
                      f"{resp.status_code} — {resp.text[:200]}")
                continue
            raw_indexes.extend(resp.json().get("indexes", []))

    except Exception as exc:
        raise RuntimeError(f"Composite index fetch failed: {exc}") from exc
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


def _deploy_indexes(indexes_path: Path, key_dict: dict) -> None:
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
        creds = service_account.Credentials.from_service_account_info(
            key_dict,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        project_id = key_dict.get("project_id", "")
    except Exception as exc:
        print(f"  [index-sync] Credentials error: {exc}")
        return

    session   = AuthorizedSession(creds)
    base_url  = f"https://firestore.googleapis.com/v1beta2/projects/{project_id}/databases/(default)"
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


def _fetch_live_field_overrides(key_dict: dict) -> list[dict]:
    """Fetch single-field index overrides via the Firestore Admin REST API.

    Calls /collectionGroups/-/fields with filter=indexConfig.usesAncestorConfig:false
    (required — without this filter the API returns only the database-wide default
    sentinel and nothing else). Skips the wildcard * field path and returns entries
    ready for the fieldOverrides section of firestore.indexes.json.
    """
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
    except ImportError as _e:
        raise RuntimeError("google-auth not installed — run: pip install google-auth requests") from _e

    print("  [index-sync] Fetching live field overrides via Firestore Admin REST API…")

    try:
        creds = service_account.Credentials.from_service_account_info(
            key_dict,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        project_id = key_dict.get("project_id", "")
        if not project_id:
            print("  [index-sync] Could not determine project_id — skipping field overrides")
            return []

        session  = AuthorizedSession(creds)
        # Firestore Admin field operations require v1beta2, not v1
        base     = (f"https://firestore.googleapis.com/v1beta2/projects/{project_id}"
                    f"/databases/(default)")
        url      = f"{base}/collectionGroups/-/fields"

        # The fields endpoint requires this filter — without it the API only
        # returns the database-wide default entry and nothing else.
        # The wildcard collectionGroups/- endpoint also does not support pageSize.
        resp = session.get(
            url,
            params={"pageSize": 0, "filter": "indexConfig.usesAncestorConfig:false"},
            timeout=60,
        )
        if not resp.ok:
            raise RuntimeError(
                f"{resp.status_code} {resp.reason} — {resp.text[:400]}"
            )
        raw_fields: list[dict] = resp.json().get("fields", [])
        print(f"  [index-sync] Raw fields from API: {len(raw_fields)}")
        # Print unique collection groups seen in raw response for debugging
        raw_cgs: set[str] = set()
        for _f in raw_fields:
            _n = _f.get("name", "")
            if "/collectionGroups/" in _n:
                raw_cgs.add(_n.split("/collectionGroups/")[1].split("/")[0])
        if raw_cgs:
            print(f"  [index-sync] CGs in raw fields response: {', '.join(sorted(raw_cgs))}")

    except Exception as exc:
        raise RuntimeError(f"Field override fetch failed: {exc}") from exc

    overrides: list[dict] = []
    for field in raw_fields:
        name = field.get("name", "")
        # name: .../collectionGroups/{cg}/fields/{fieldPath}
        if "/collectionGroups/" not in name or "/fields/" not in name:
            continue
        cg        = name.split("/collectionGroups/")[1].split("/")[0]
        field_path = name.split("/fields/")[1]

        # Skip the database-wide __default__ sentinel
        if field_path == "*":
            continue

        idx_cfg = field.get("indexConfig", {})
        indexes = idx_cfg.get("indexes", [])
        if not indexes:
            continue

        clean_indexes = []
        for idx in indexes:
            state = idx.get("state", "READY")
            if state not in ("READY", ""):
                continue
            entry: dict = {"queryScope": idx.get("queryScope", "COLLECTION")}
            # v1 format: order/arrayConfig directly on the index object
            if "order" in idx:
                entry["order"] = idx["order"]
            elif "arrayConfig" in idx:
                entry["arrayConfig"] = idx["arrayConfig"]
            # v1beta2 format: order/arrayConfig nested inside fields[0]
            elif idx.get("fields"):
                f0 = idx["fields"][0]
                if "order" in f0:
                    entry["order"] = f0["order"]
                elif "arrayConfig" in f0:
                    entry["arrayConfig"] = f0["arrayConfig"]
                else:
                    continue
            else:
                continue
            clean_indexes.append(entry)

        if clean_indexes:
            overrides.append({
                "collectionGroup": cg,
                "fieldPath":       field_path,
                "indexes":         clean_indexes,
            })

    print(f"  [index-sync] {len(overrides)} field overrides fetched from Firestore")
    return overrides


def _override_key(ov: dict) -> str:
    return f"{ov['collectionGroup']}:{ov['fieldPath']}"


def _merge_overrides(existing: dict, live: list[dict]) -> tuple[dict, int, int]:
    """Merge live field overrides into existing, return (merged, added, skipped)."""
    seen    = {_override_key(ov): True for ov in existing.get("fieldOverrides", [])}
    added   = 0
    skipped = 0
    merged  = list(existing.get("fieldOverrides", []))

    for ov in live:
        key = _override_key(ov)
        if key in seen:
            skipped += 1
        else:
            merged.append(ov)
            seen[key] = True
            added += 1

    result = dict(existing)
    result["fieldOverrides"] = merged
    return result, added, skipped

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
        from collections import defaultdict as _dd
        ov_groups: dict = _dd(list)
        for ov in overrides:
            ov_groups[ov.get("collectionGroup")].append(ov)
        for cg in sorted(ov_groups):
            print(f"\n  {cg}  [field overrides]")
            for ov in ov_groups[cg]:
                modes = "  ".join(
                    f"({'↑' if i.get('order') == 'ASCENDING' else '↓' if i.get('order') == 'DESCENDING' else i.get('arrayConfig','?')})"
                    for i in ov.get("indexes", [])
                )
                print(f"    · {ov.get('fieldPath'):<40} {modes}")

    print("══════════════════════════════════════════════════════════════════\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    """Return 0 on success, 1 on any failure (so Firebase predeploy hook aborts deploy)."""
    import sys
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    ap = argparse.ArgumentParser(
        description="Sync live Firestore indexes into firestore.indexes.json"
    )
    ap.add_argument("--output",        default=None, metavar="FILE",
                    help="Path to firestore.indexes.json  "
                         "(default: project root / firestore.indexes.json)")
    ap.add_argument("--dry-run",       action="store_true",
                    help="Print merged JSON without writing the file")
    ap.add_argument("--discover-only", action="store_true",
                    help="List collections/subcollections, merge + write index file, then exit")
    ap.add_argument("--no-discover",   action="store_true",
                    help="Skip collection/subcollection browsing — index fetch still runs")
    ap.add_argument("--deploy",        action="store_true",
                    help="Deploy indexes via REST API after writing the file")
    args = ap.parse_args(argv)

    output_path = Path(args.output) if args.output else (
        Path(__file__).resolve().parent / "firestore.indexes.json"
    )

    # ── Load credentials — hard-fail so predeploy aborts immediately ────────
    try:
        key_dict = _load_key_dict()   # raw dict — for REST API calls
        cert     = _load_secrets()    # Certificate — for Admin SDK
    except Exception as exc:
        print(f"  [index-sync] FATAL: could not load credentials: {exc}", flush=True)
        return 1

    # ── Firestore collection discovery (Admin SDK, optional) ─────────────────
    if not args.no_discover:
        try:
            db  = _init_firestore(cert)
            top = discover_collections(db)
            discover_subcollections(db, list(top.keys()))
        except Exception as exc:
            print(f"\n  [index-sync] Discovery failed (non-fatal): {exc}")

    # ── Load existing file first so we can seed CG discovery ───────────────
    print(f"  [index-sync] Loading {output_path.name}…", flush=True)
    existing     = _load_existing(output_path)
    existing_cgs = {idx["collectionGroup"] for idx in existing.get("indexes", [])}

    # ── Fetch live indexes + field overrides — hard-fail on any error ────────
    print("\n  [index-sync] Fetching live data from Firestore…", flush=True)
    try:
        live_indexes = _fetch_live_indexes(key_dict, hint_cgs=existing_cgs)
    except Exception as exc:
        print(f"  [index-sync] FATAL: {exc}", flush=True)
        return 1
    try:
        live_overrides = _fetch_live_field_overrides(key_dict)
    except Exception as exc:
        print(f"  [index-sync] FATAL: {exc}", flush=True)
        return 1

    if not live_indexes and not live_overrides:
        print("  [index-sync] FATAL: Firestore returned 0 indexes and 0 field overrides — "
              "aborting to avoid overwriting the index file with empty data.", flush=True)
        return 1

    # ── Merge into local file ───────────────────────────────────────────────
    merged,   added_idx, _ = _merge(existing, live_indexes)
    merged, added_ovr, _   = _merge_overrides(merged, live_overrides)

    from_idx   = len(existing.get("indexes", []))
    from_ovr   = len(existing.get("fieldOverrides", []))
    total_idx  = len(merged["indexes"])
    total_ovr  = len(merged["fieldOverrides"])
    print(f"  [index-sync] Composite indexes — was: {from_idx}  added: {added_idx}  total: {total_idx}")
    print(f"  [index-sync] Field overrides   — was: {from_ovr}  added: {added_ovr}  total: {total_ovr}")

    if args.dry_run:
        print("\n── firestore.indexes.json (dry-run — not written) ─────────────")
        print(json.dumps(merged, indent=2, ensure_ascii=False))
        _print_summary(merged)
        return 0

    # ── Write file ──────────────────────────────────────────────────────────
    try:
        merged_json = json.dumps(merged, indent=2, ensure_ascii=False)
        output_path.write_text(merged_json + "\n", encoding="utf-8")
    except Exception as exc:
        print(f"  [index-sync] FATAL: could not write {output_path}: {exc}", flush=True)
        return 1

    # ── Verify the file is valid and complete before proceeding ─────────────
    try:
        written = json.loads(output_path.read_text(encoding="utf-8"))
        assert len(written.get("indexes", [])) == total_idx, "index count mismatch after write"
        assert len(written.get("fieldOverrides", [])) == total_ovr, "override count mismatch after write"
    except Exception as exc:
        print(f"  [index-sync] FATAL: post-write verification failed: {exc}", flush=True)
        return 1

    print(f"  [index-sync] ✓ {output_path.name} written and verified "
          f"({total_idx} indexes, {total_ovr} overrides)", flush=True)

    _print_summary(merged)

    if args.discover_only:
        return 0

    # ── Optional REST deploy ─────────────────────────────────────────────────
    if args.deploy:
        _deploy_indexes(output_path, key_dict)

    return 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main())
