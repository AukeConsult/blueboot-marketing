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
from functions.config import cfg


def _get_db():
    import firebase_admin, firebase_admin.credentials as fb_creds
    from firebase_admin import firestore

    from dotenv import load_dotenv; load_dotenv()
    from functions.firebase_cred import get_firebase_cred
    cred = get_firebase_cred()
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
        gen = query.stream()
        while True:
            try:
                doc = next(gen)
            except StopIteration:
                break
            except (ValueError, AttributeError):
                continue  # skip _rowy_ and unrelated collection docs
            try:
                d = doc.to_dict() or {}
            except Exception:
                continue
            if not d:
                continue
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
    collection = args.collection or cfg.FIRESTORE_COLLECTION
    search(args.keyword, args.field, args.country, args.limit, collection)


if __name__ == "__main__":
    main()
