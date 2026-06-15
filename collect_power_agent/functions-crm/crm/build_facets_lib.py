"""build_facets_lib.py -- Worker-side builder for the base filter-facets docs.

Scans the data and writes the catalog of selectable filter values to
filter_facets/<doc_name>:

  * pipeline == "site_leads" -> doc 'site_leads' (site_leads + site_contacts)
  * pipeline == "leads"      -> doc 'leads'      (leads + email_contacts[mark_leads])

This is the Cloud-Run / crmWorker counterpart of app/build_filter_facets.py.
Keep the field lists here in sync with:
  * app/build_filter_facets.py        (the CLI companion)
  * crm/filter_count_lib.py           (count field lists)
  * crm/facet_campaign_lib.py         (copy field specs)
"""
from __future__ import annotations

import os
import re
from collections import Counter
from datetime import datetime, timezone

from google.cloud.firestore_v1.base_query import FieldFilter

FILTER_FACETS_COLLECTION = "filter_facets"
COLLECTION_DEFAULT = "site_leads"
CONTACTS_SUBCOLLECTION = "site_contacts"
LEADS_COLLECTION = "leads"
EMAIL_CONTACTS_COLLECTION = "email_contacts"

# high-cardinality caps (env-overridable, same defaults as the CLI)
TOP_N_LOCATION = int(os.getenv("FACET_TOP_N_LOCATION", "200"))
TOP_N_KEYWORDS = int(os.getenv("FACET_TOP_N_KEYWORDS", "100"))
TOP_N_AI_PLATFORM = int(os.getenv("FACET_TOP_N_AI_PLATFORM", "10"))
TITLE_MIN_COUNT = int(os.getenv("FACET_TITLE_MIN_COUNT", "20"))

# Canonical page-count size bands (kept in sync with build_filter_facets.py).
PAGE_GROUPS: list = [
    ("micro",  "micro (1-50)",      1,      50),
    ("small",  "small (51-500)",    51,     500),
    ("medium", "medium (501-3k)",   501,    3000),
    ("large",  "large (3k-10k)",    3001,   10000),
    ("huge",   "huge (10k-100k)",   10001,  100000),
    ("ultra",  "ultra (100k+)",     100001, None),
]


def _first_word(value) -> str:
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


def _to_list(val) -> list:
    if isinstance(val, list):
        return [str(v).strip() for v in val if str(v).strip()]
    if isinstance(val, str) and val.strip():
        return [v.strip() for v in re.split(r"[,;|\n]", val) if v.strip()]
    return []


class EnumFacet:
    """Distinct values with counts. Owns its own counter; never raises on add."""

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
        kept = [(v, c) for v, c in self._counts.most_common() if c >= self.min_count]
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


def build_site_leads_facets(db, collection: str, cap: int) -> dict:
    """Build the site_leads catalog (site_leads + site_contacts)."""
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

    occupation   = EnumFacet(cap, lower=True)
    title        = EnumFacet(cap, lower=True, transform=_first_word, min_count=TITLE_MIN_COUNT)
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
        "pipeline": "site_leads",
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


def build_leads_facets(db, cap: int) -> dict:
    """Build the leads catalog (leads + email_contacts[mark_leads])."""
    ai_sector        = EnumFacet(cap, lower=True)
    ai_company_type  = EnumFacet(cap, lower=True)
    ai_platform      = EnumFacet(TOP_N_AI_PLATFORM, lower=True)
    country          = EnumFacet(cap)
    ai_reseller_pot  = EnumFacet(cap, lower=True)
    ai_client_base   = EnumFacet(cap, lower=True)
    ai_specialisation = EnumFacet(cap, kind="array_enum", lower=True)

    lead_count = 0
    for doc in db.collection(LEADS_COLLECTION).select(
        ["ai_sector", "ai_company_type", "ai_platform", "country",
         "ai_reseller_potential", "ai_client_base", "ai_specialisation", "detected_tech"]
    ).stream():
        data = doc.to_dict() or {}
        lead_count += 1
        ai_sector.add(data.get("ai_sector"))
        ai_company_type.add(data.get("ai_company_type"))
        ai_platform.add(data.get("ai_platform"))
        country.add(data.get("country"))
        ai_reseller_pot.add(data.get("ai_reseller_potential"))
        ai_client_base.add(data.get("ai_client_base"))
        ai_specialisation.add_many(_to_list(data.get("ai_specialisation")))

    title      = EnumFacet(cap, lower=True, transform=_first_word, min_count=TITLE_MIN_COUNT)
    email_type = EnumFacet(cap)
    contact_count = 0
    for doc in db.collection(EMAIL_CONTACTS_COLLECTION).where(
            filter=FieldFilter("mark_leads", "==", True)).select(
            ["title", "email_type"]).stream():
        data = doc.to_dict() or {}
        contact_count += 1
        title.add(data.get("title"))
        email_type.add(data.get("email_type"))

    return {
        "generated_at":       datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_collection":  LEADS_COLLECTION,
        "pipeline":           "leads",
        "lead_count":         lead_count,
        "contact_count":      contact_count,
        "value_cap_per_field": cap,
        "filters": {
            "ai_sector":            ai_sector.result("leads.ai_sector"),
            "ai_company_type":      ai_company_type.result("leads.ai_company_type"),
            "ai_platform":          ai_platform.result("leads.ai_platform"),
            "country":              country.result("leads.country"),
            "ai_reseller_potential": ai_reseller_pot.result("leads.ai_reseller_potential"),
            "ai_client_base":       ai_client_base.result("leads.ai_client_base"),
            "ai_specialisation":    ai_specialisation.result("leads.ai_specialisation"),
            "title":                title.result("email_contacts[mark_leads].title"),
            "email_type":           email_type.result("email_contacts[mark_leads].email_type"),
        },
    }


def run_build_facets(db, pipeline: str = "site_leads", cap: int = 300) -> dict:
    """Build and store the base filter-facets doc for a pipeline.

    Writes filter_facets/<doc_name> (doc_name == pipeline) and returns a summary.
    """
    pipeline = (pipeline or "site_leads").strip().lower()
    if pipeline not in ("site_leads", "leads"):
        raise ValueError(f"pipeline must be 'site_leads' or 'leads', got '{pipeline}'")
    cap = int(cap or 300)

    if pipeline == "leads":
        facets = build_leads_facets(db, cap)
        doc_name = "leads"
    else:
        facets = build_site_leads_facets(db, COLLECTION_DEFAULT, cap)
        doc_name = COLLECTION_DEFAULT

    db.collection(FILTER_FACETS_COLLECTION).document(doc_name).set(facets, merge=False)

    return {
        "pipeline":      pipeline,
        "doc":           doc_name,
        "lead_count":    facets["lead_count"],
        "contact_count": facets["contact_count"],
        "fields":        list(facets["filters"].keys()),
        "generated_at":  facets["generated_at"],
    }
