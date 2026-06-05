"""build_filter_facets.py -- Scan site_leads + their site_contacts subcollection,
merge them, and read out the fields that make good filter inputs.

The result is a single "filter facets" document: the catalog of selectable values a
later filter function/UI reads from.

site_leads enums:
  * platform     -- scraped CMS (woocommerce / shopify / wordpress / "")
  * ai_platform  -- AI-inferred CMS / site builder (top 10 most-used)
  * ai_sector    -- AI-inferred sector (manufacturing / technology / public_sector / ...)
  * ai_company_type -- AI-inferred company type (B2B / government / media / ...)
  * location     -- AI-inferred "City, Country" (top 200 most-used)
  * location_country -- AI-inferred country of the company HQ
site_leads array enum:
  * keywords     -- list field, flattened + lowercased; top 100 most-used values
site_leads group:
  * page_count   -- canonical size buckets from maint_statistics.py; each band exposes
                    min/max so a selection maps straight onto min_pages/max_pages.
site_contacts enums:
  * occupation   -- confirmed job role
  * title        -- job title -> first word, lowercased; values with count < 20 dropped
  * email_type   -- personal / role / department / admin
merged across both:
  * country      -- site_leads.country + site_contacts.country
  * ai_country   -- site_leads.ai_country + site_contacts.ai_country

Outputs (both written):
  1. Firestore:  filter_facets/{collection}
  2. JSON file:  output/filter_facets_{collection}.json

Usage:
    python app/build_filter_facets.py
    python app/build_filter_facets.py --cap 300
    python app/build_filter_facets.py --no-write          # JSON only, skip Firestore

Synchronous, single-threaded read script run before any event loop -- no asyncio
timeouts or locks needed (sync Firestore reads at startup are allowed by project rules).
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import _pathsetup  # noqa: F401  -- sets Windows selector loop / sys.path

COLLECTION_DEFAULT = "site_leads"
CONTACTS_SUBCOLLECTION = "site_contacts"
OUTPUT_DIR = Path(__file__).parent.parent / "output"
# high-cardinality fields -- return only the N most-used values.
TOP_N_LOCATION = 200
TOP_N_KEYWORDS = 100
TOP_N_AI_PLATFORM = 10

# Canonical page-count size bands -- kept in sync with the buckets in
# app/maint_statistics.py. (key, label, lo, hi) where the band matches lo <= pc <= hi.
# hi=None means open-ended (no upper bound). "unknown" captures 0 / None / unparseable.
PAGE_GROUPS: list[tuple[str, str, int, "int | None"]] = [
    ("micro",  "micro (1-50)",      1,      50),
    ("small",  "small (51-500)",    51,     500),
    ("medium", "medium (501-3k)",   501,    3000),
    ("large",  "large (3k-10k)",    3001,   10000),
    ("huge",   "huge (10k-100k)",   10001,  100000),
    ("ultra",  "ultra (100k+)",     100001, None),
]


def _first_word(value) -> str:
    """Leading run of letters only -- stops at the first space, digit or any
    special character. Unicode-aware (keeps ae/oe/aa etc). 'Co-Founder' -> 'Co',
    'Sales2024' -> 'Sales', 'Daglig leder' -> 'Daglig'."""
    m = re.match(r"[^\W\d_]+", str(value or "").strip(), re.UNICODE)
    return m.group(0) if m else ""


def _page_group_key(pc) -> str:
    try:
        pc = int(pc)
    except (TypeError, ValueError):
        return "unknown"
    if pc <= 0:
        return "unknown"
    for key, _label, lo, hi in PAGE_GROUPS:
        if pc >= lo and (hi is None or pc <= hi):
            return key
    return "unknown"


class EnumFacet:
    """Distinct values with counts. Owns its own counter; never raises on add.

    kind="enum"        -> scalar field (one value per doc)
    kind="array_enum"  -> list field (flatten elements via add_many)
    """

    def __init__(self, cap: int, kind: str = "enum", lower: bool = False,
                 transform=None, min_count: int = 0) -> None:
        self.cap = cap
        self.kind = kind
        self.lower = lower
        self.transform = transform
        self.min_count = min_count
        self._counts: Counter = Counter()

    def add(self, value, weight: int = 1) -> None:
        if value is None:
            return
        s = str(value).strip()
        if self.transform:
            s = self.transform(s)
        if self.lower:
            s = s.lower()
        if s:
            self._counts[s] += weight

    def add_many(self, values) -> None:
        for v in (values or []):
            self.add(v)

    def merge(self, other: "EnumFacet") -> None:
        self._counts.update(other._counts)

    def result(self, source: str) -> dict:
        kept = [(v, c) for v, c in self._counts.most_common()
                if c >= self.min_count]
        return {
            "type": self.kind,
            "source": source,
            "distinct": len(kept),
            "min_count": self.min_count,
            "truncated": len(kept) > self.cap,
            "values": [
                {"value": v, "count": c, "selected": False}
                for v, c in kept[:self.cap]
            ],
        }


class PageGroupFacet:
    """Buckets page_count into the canonical size bands; never raises on add."""

    def __init__(self) -> None:
        self._counts: Counter = Counter()

    def add(self, page_count) -> None:
        self._counts[_page_group_key(page_count)] += 1

    def result(self, source: str) -> dict:
        groups = [
            {"key": key, "label": label, "min": lo, "max": hi,
             "count": self._counts.get(key, 0)}
            for key, label, lo, hi in PAGE_GROUPS
        ]
        groups.append({
            "key": "unknown", "label": "unknown (0/None)", "min": 0, "max": 0,
            "count": self._counts.get("unknown", 0),
        })
        return {"type": "group", "source": source, "groups": groups}


def _get_db():
    try:
        from app.firestore_client import get_firestore
    except ImportError:
        from firestore_client import get_firestore
    return get_firestore()


def build_facets(collection: str, cap: int) -> dict:
    db = _get_db()

    # site_leads facets
    platform     = EnumFacet(cap)
    ai_platform  = EnumFacet(TOP_N_AI_PLATFORM, lower=True)
    ai_sector    = EnumFacet(cap, lower=True)
    ai_company_type = EnumFacet(cap, lower=True)
    location     = EnumFacet(TOP_N_LOCATION, lower=True)
    location_country = EnumFacet(cap)
    keywords     = EnumFacet(TOP_N_KEYWORDS, kind="array_enum", lower=True)
    pages        = PageGroupFacet()
    country_leads = EnumFacet(cap)
    ai_country_leads = EnumFacet(cap)

    # site_contacts facets
    occupation   = EnumFacet(cap, lower=True)
    title        = EnumFacet(cap, lower=True, transform=_first_word, min_count=20)
    email_type   = EnumFacet(cap)
    country_contacts = EnumFacet(cap)
    ai_country_contacts = EnumFacet(cap)

    lead_count = 0
    for doc in db.collection(collection).select(
        ["platform", "ai_platform", "ai_sector", "ai_company_type",
         "country", "ai_country", "location", "location_country",
         "keywords", "page_count"]
    ).stream():
        data = doc.to_dict() or {}
        lead_count += 1
        platform.add(data.get("platform"))
        ai_platform.add(data.get("ai_platform"))
        ai_sector.add(data.get("ai_sector"))
        ai_company_type.add(data.get("ai_company_type"))
        location.add(data.get("location"))
        location_country.add(data.get("location_country"))
        keywords.add_many(data.get("keywords"))
        pages.add(data.get("page_count"))
        country_leads.add(data.get("country"))
        ai_country_leads.add(data.get("ai_country"))

    contact_count = 0
    for doc in db.collection_group(CONTACTS_SUBCOLLECTION).select(
        ["country", "ai_country", "occupation", "title", "email_type"]
    ).stream():
        data = doc.to_dict() or {}
        contact_count += 1
        occupation.add(data.get("occupation"))
        title.add(data.get("title"))
        email_type.add(data.get("email_type"))
        country_contacts.add(data.get("country"))
        ai_country_contacts.add(data.get("ai_country"))

    # Merge country / ai_country across both collections into one enum each.
    country_merged = EnumFacet(cap)
    country_merged.merge(country_leads)
    country_merged.merge(country_contacts)
    ai_country_merged = EnumFacet(cap)
    ai_country_merged.merge(ai_country_leads)
    ai_country_merged.merge(ai_country_contacts)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_collection": collection,
        "contacts_subcollection": CONTACTS_SUBCOLLECTION,
        "lead_count": lead_count,
        "contact_count": contact_count,
        "value_cap_per_field": cap,
        "filters": {
            "platform":    platform.result("site_leads.platform"),
            "ai_platform": ai_platform.result("site_leads.ai_platform"),
            "ai_sector":   ai_sector.result("site_leads.ai_sector"),
            "ai_company_type": ai_company_type.result("site_leads.ai_company_type"),
            "country":     country_merged.result(
                "merged: site_leads.country + site_contacts.country"),
            "ai_country":  ai_country_merged.result(
                "merged: site_leads.ai_country + site_contacts.ai_country"),
            "location":    location.result("site_leads.location"),
            "location_country": location_country.result("site_leads.location_country"),
            "keywords":    keywords.result("site_leads.keywords"),
            "page_count":  pages.result("site_leads.page_count"),
            "occupation":  occupation.result("site_contacts.occupation"),
            "title":       title.result("site_contacts.title"),
            "email_type":  email_type.result("site_contacts.email_type"),
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Build a filter-facets catalog from site_leads + site_contacts.")
    ap.add_argument("--collection", default=COLLECTION_DEFAULT,
                    help="Top-level leads collection (default: site_leads)")
    ap.add_argument("--cap", type=int, default=300,
                    help="Max distinct values stored per enum field (default: 300)")
    ap.add_argument("--no-write", action="store_true",
                    help="Skip the Firestore write; only emit the JSON file")
    args = ap.parse_args()

    print(f"[facets] scanning '{args.collection}' + '{CONTACTS_SUBCOLLECTION}' ...")
    facets = build_facets(args.collection, args.cap)
    print(f"[facets] {facets['lead_count']} leads, {facets['contact_count']} contacts")
    for name, f in facets["filters"].items():
        if "values" in f:
            flag = "  (TRUNCATED -- high cardinality for an enum)" if f["truncated"] else ""
            print(f"  - {name} [{f['type']}]: {f['distinct']} distinct{flag}")
        else:
            print(f"  - {name} [group]: {len(f['groups'])} groups")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUTPUT_DIR / f"filter_facets_{args.collection}.json"
    json_path.write_text(json.dumps(facets, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[facets] wrote {json_path}")

    if args.no_write:
        # Dry run: print the full result document to the console for inspection.
        print(json.dumps(facets, indent=2, ensure_ascii=False))
    else:
        try:
            db = _get_db()
            db.collection("filter_facets").document(args.collection).set(facets, merge=False)
            print(f"[facets] wrote Firestore filter_facets/{args.collection}")
        except Exception as exc:
            print(f"[facets] Firestore write failed ({exc}) -- JSON file is still available")


if __name__ == "__main__":
    main()
