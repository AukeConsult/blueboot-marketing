"""Quick Firestore snapshot tool — search leads by keyword in any field.

Usage:
    python app\firestore_snapshot.py wordpress
    python app\firestore_snapshot.py wordpress --field source_query
    python app\firestore_snapshot.py wordpress --limit 20
    python app\firestore_snapshot.py wordpress --country NO
"""
from __future__ import annotations
import threading as _threading

# Guards firebase_admin.initialize_app against concurrent init
_local_fb_lock = _threading.Lock()
import argparse, importlib.util, json, os, sys
from pathlib import Path

import _pathsetup  # noqa: F401


def _get_db():
    import firebase_admin, firebase_admin.credentials as fb_creds
    from firebase_admin import firestore

    secrets_path = Path(__file__).parent.parent / "blueboot_secrets.py"
    key_dict = None
    if secrets_path.exists():
        spec = importlib.util.spec_from_file_location("blueboot_secrets", secrets_path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        key_dict = getattr(mod, "fireBaseAdminKey", None)

    cred = (fb_creds.Certificate(key_dict) if key_dict
            else fb_creds.Certificate(os.getenv("FIREBASE_CREDENTIALS",
                                                "config/serviceAccountKey.json")))
    with _local_fb_lock:
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
    return firestore.client()


SEARCH_FIELDS = [
    "source_query", "reasons", "keywords", "title", "description",
    "company", "domain", "website",
]

DISPLAY_FIELDS = [
    "domain", "country", "country_name", "country_original",
    "reseller_score", "priority",
    "source_query", "found_by_search", "found_by_catalog",
    "title", "company", "website",
    "reasons",
    "keywords",
]


def search(keyword: str, field: str | None, country: str | None,
           limit: int, collection: str) -> None:
    db  = _get_db()
    col = db.collection(collection)

    keyword_l = keyword.lower()
    fields_to_check = [field] if field else SEARCH_FIELDS

    matches: list[dict] = []

    # Use a Firestore range query on source_query when that's the only field
    # (avoids a full collection scan for the common case).
    if fields_to_check == ["source_query"]:
        query = (col
                 .where("source_query", ">=", keyword)
                 .where("source_query", "<=", keyword + ""))
        if country:
            query = query.where("country", "==", country.upper())
        for doc in query.limit(limit).stream():
            matches.append(doc.to_dict())
    else:
        # Full scan with client-side filter
        query = col
        if country:
            query = col.where("country", "==", country.upper())
        for doc in query.stream():
            d = doc.to_dict()
            haystack = " ".join(str(d.get(f) or "") for f in fields_to_check).lower()
            if keyword_l in haystack:
                matches.append(d)
            if len(matches) >= limit:
                break

    print(f"\n{'='*60}")
    print(f"  Keyword : '{keyword}'")
    print(f"  Field(s): {', '.join(fields_to_check)}")
    print(f"  Country : {country or 'all'}")
    print(f"  Results : {len(matches)} (limit {limit})")
    print(f"{'='*60}\n")

    for i, d in enumerate(matches, 1):
        print(f"── Lead {i} ──────────────────────────────────────────────")
        for key in DISPLAY_FIELDS:
            val = d.get(key)
            if val is None:
                continue
            if isinstance(val, list):
                val = ", ".join(str(v) for v in val)
            print(f"  {key:<20} {val}")
        print()


def main():
    p = argparse.ArgumentParser(description="Search Firestore leads by keyword")
    p.add_argument("keyword", help="Keyword to search for")
    p.add_argument("--field",      default=None,
                   help=f"Restrict search to one field (default: all). "
                        f"Options: {', '.join(SEARCH_FIELDS)}")
    p.add_argument("--country",    default=None, help="Filter by country code, e.g. NO")
    p.add_argument("--limit",      type=int, default=10, help="Max results (default: 10)")
    p.add_argument("--collection", default=None,
                   help="Firestore collection (default: leads / FIRESTORE_COLLECTION env)")
    args = p.parse_args()

    collection = args.collection or os.getenv("FIRESTORE_COLLECTION", "leads")
    search(args.keyword, args.field, args.country, args.limit, collection)


if __name__ == "__main__":
    main()
