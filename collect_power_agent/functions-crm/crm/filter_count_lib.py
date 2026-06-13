"""filter_count_lib.py -- Count site_leads + site_contacts that match a saved
filter-facets selection, and write the counts back into the doc.

Used by the crmWorker 'filter-count' job (enqueued when a filter preset is saved).

A "selection" is the set of values flagged selected:true inside
filter_facets/<name>.filters. Semantics mirror app/filter_site_leads.py:
  * within one category -> OR (match any selected value)
  * across categories   -> AND
  * a category with no selected values is ignored (matches everything)
The contact `title` facet is stored as first-word + lowercased, so the same
transform is applied to each contact's title before comparing.
"""
from __future__ import annotations

from google.cloud.firestore_v1.base_query import FieldFilter

import os
import re
from collections import Counter
from datetime import datetime, timezone

FILTER_FACETS_COLLECTION = "filter_facets"
LEADS_COLLECTION = "site_leads"
CONTACTS_SUBCOLLECTION = "site_contacts"
EMAIL_CONTACTS_COLLECTION = "email_contacts"

LEAD_SCALAR_FIELDS = (
    "platform", "ai_platform", "ai_sector", "ai_company_type",
    "country", "ai_country", "location", "location_country",
)
LEAD_ARRAY_FIELDS = ("keywords",)
CONTACT_FIELDS = ("occupation", "title", "email_type")
GROUP_FIELD = "page_count"
TOP_N_KEYWORDS = int(os.getenv("FACET_TOP_N_KEYWORDS", "100"))   # keep in sync with build_filter_facets.py

# (key, lo, hi) -- canonical page bands, kept in sync with build_filter_facets.py
_PAGE_BANDS = [
    ("micro", 1, 50), ("small", 51, 500), ("medium", 501, 3000),
    ("large", 3001, 10000), ("huge", 10001, 100000), ("ultra", 100001, None),
]


def _to_list(val) -> list:
    """Coerce string-or-list field into a list (handles comma-separated strings)."""
    import re as _re
    if isinstance(val, list):
        return [str(v).strip().lower() for v in val if str(v).strip()]
    if isinstance(val, str) and val.strip():
        return [v.strip().lower() for v in _re.split(r"[,;|\n]", val) if v.strip()]
    return []


def _page_key(pc) -> str:
    try:
        pc = int(pc)
    except (TypeError, ValueError):
        return "unknown"
    if pc <= 0:
        return "unknown"
    for key, lo, hi in _PAGE_BANDS:
        if pc >= lo and (hi is None or pc <= hi):
            return key
    return "unknown"


def _first_word(value) -> str:
    m = re.match(r"[^\W\d_]+", str(value or "").strip(), re.UNICODE)
    return m.group(0) if m else ""


def _email_doc_id(email) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", str(email or "").strip().lower())


def _starts_any(value, keys) -> bool:
    """True if the (lowercased) real value starts with ANY selected key."""
    v = str(value or "").strip().lower()
    return any(v.startswith(k) for k in keys)


def _selected_values(facet) -> set:
    return {str(x.get("value")).strip().lower()
            for x in (facet.get("values") or []) if x.get("selected")}


def _selected_groups(facet) -> set:
    return {str(g.get("key")).strip().lower()
            for g in (facet.get("groups") or []) if g.get("selected")}


def _refresh_keywords(db, filters: dict) -> None:
    """Re-extract the keywords facet from site_leads (lowercased, top N by
    count), preserving which values were selected. Previously-selected
    keywords that fall out of the top N are kept so a selection is never lost.
    Mutates filters['keywords'] in place."""
    prev = filters.get("keywords", {}) or {}
    prev_selected = {str(v.get("value")).strip().lower()
                     for v in prev.get("values", []) if v.get("selected")}
    counter: Counter = Counter()
    for d in db.collection(LEADS_COLLECTION).select(["keywords"]).stream():
        for kw in (d.to_dict() or {}).get("keywords") or []:
            w = str(kw).strip().lower()
            if w:
                counter[w] += 1
    values = [{"value": v, "count": c, "selected": v in prev_selected}
              for v, c in counter.most_common(TOP_N_KEYWORDS)]
    present = {v["value"] for v in values}
    for kw in prev_selected:
        if kw not in present:
            values.append({"value": kw, "count": counter.get(kw, 0), "selected": True})
    filters["keywords"] = {
        "type":      "array_enum",
        "source":    "site_leads.keywords",
        "distinct":  len(counter),
        "min_count": 0,
        "truncated": len(counter) > TOP_N_KEYWORDS,
        "values":    values,
    }


def run_filter_count(db, name: str) -> dict:
    """Count matching leads/contacts for filter_facets/<name> and store results."""
    if not name:
        raise ValueError("filter-count job requires a 'name'")
    doc_ref = db.collection(FILTER_FACETS_COLLECTION).document(name)
    snap = doc_ref.get()
    if not snap.exists:
        raise ValueError(f"filter_facets/'{name}' not found")
    filters = (snap.to_dict() or {}).get("filters", {}) or {}

    # Step 1: refresh the keyword list from current data (selections preserved).
    _refresh_keywords(db, filters)

    lead_scalar = {f: _selected_values(filters[f])
                   for f in LEAD_SCALAR_FIELDS
                   if f in filters and _selected_values(filters[f])}
    lead_array = {f: _selected_values(filters[f])
                  for f in LEAD_ARRAY_FIELDS
                  if f in filters and _selected_values(filters[f])}
    contact_sel = {f: _selected_values(filters[f])
                   for f in CONTACT_FIELDS
                   if f in filters and _selected_values(filters[f])}
    page_keys = _selected_groups(filters[GROUP_FIELD]) if GROUP_FIELD in filters else set()

    def lead_match(d: dict) -> bool:
        for f, sel in lead_scalar.items():
            if not _starts_any(d.get(f), sel):
                return False
        for f, sel in lead_array.items():
            have = [v.lower() for v in _to_list(d.get(f))]
            if not any(h.startswith(k) for h in have for k in sel):
                return False
        if page_keys and _page_key(d.get("page_count")) not in page_keys:
            return False
        return True

    def contact_match(d: dict) -> bool:
        for f, sel in contact_sel.items():
            if not _starts_any(d.get(f), sel):
                return False
        return True

    # 1. lead-level candidates (sites whose own fields match the selection)
    lead_fields = list(LEAD_SCALAR_FIELDS) + list(LEAD_ARRAY_FIELDS) + [GROUP_FIELD, "lead_id"]
    candidate_leads: set = set()
    # per-value counters for matched leads -- used to write selected_count back
    lead_val_counts: dict[str, Counter] = {
        f: Counter() for f in list(LEAD_SCALAR_FIELDS) + list(LEAD_ARRAY_FIELDS) + [GROUP_FIELD]
    }
    for d in db.collection(LEADS_COLLECTION).select(lead_fields).stream():
        data = d.to_dict() or {}
        if lead_match(data):
            candidate_leads.add(data.get("lead_id") or d.id)
            for f in LEAD_SCALAR_FIELDS:
                v = str(data.get(f) or "").strip().lower()
                if v:
                    lead_val_counts[f][v] += 1
            for f in LEAD_ARRAY_FIELDS:
                for kw in (data.get(f) or []):
                    w = str(kw).strip().lower()
                    if w:
                        lead_val_counts[f][w] += 1
            lead_val_counts[GROUP_FIELD][_page_key(data.get("page_count"))] += 1

    # 2. email_contacts doc-ids (existence check)
    email_ids: set = set()
    for d in db.collection(EMAIL_CONTACTS_COLLECTION).select([]).stream():
        email_ids.add(d.id)

    # 3. Count the CONTACTS first: a contact counts only if its own lead is a
    #    candidate AND it passes the contact filters. A site is then counted only
    #    if it has >= 1 such contact -- so the leads and contacts totals always
    #    reflect matching sites and contacts that belong to each other.
    n_contacts = 0
    matched_sites: set = set()          # sites that have >=1 matching contact
    seen_in: set = set()
    seen_not: set = set()
    contact_val_counts: dict[str, Counter] = {f: Counter() for f in CONTACT_FIELDS}
    for d in db.collection_group(CONTACTS_SUBCOLLECTION).select(
            ["lead_id", "email", "occupation", "title", "email_type"]).stream():
        data = d.to_dict() or {}
        lid = data.get("lead_id")
        if not lid or lid not in candidate_leads:
            continue
        if contact_sel and not contact_match(data):
            continue
        n_contacts += 1
        matched_sites.add(lid)
        cid = _email_doc_id(data.get("email"))
        (seen_in if cid in email_ids else seen_not).add(cid)
        for f in CONTACT_FIELDS:
            raw = str(data.get(f) or "").strip().lower()
            v = _first_word(raw) if f == "title" else raw
            if v:
                contact_val_counts[f][v] += 1

    counts = {
        "leads":                          len(matched_sites),
        "contacts":                       n_contacts,
        "lead_candidates":                len(candidate_leads),
        "contacts_in_email_contacts":     len(seen_in),
        "contacts_not_in_email_contacts": len(seen_not),
    }
    # Step 3: write selected_count onto every facet value from the matched set.
    for f in list(LEAD_SCALAR_FIELDS) + list(LEAD_ARRAY_FIELDS):
        for val in (filters.get(f) or {}).get("values", []):
            v = str(val.get("value") or "").strip().lower()
            val["selected_count"] = lead_val_counts[f].get(v, 0)
    for grp in (filters.get(GROUP_FIELD) or {}).get("groups", []):
        k = str(grp.get("key") or "").strip().lower()
        grp["selected_count"] = lead_val_counts[GROUP_FIELD].get(k, 0)
    for f in CONTACT_FIELDS:
        for val in (filters.get(f) or {}).get("values", []):
            v = str(val.get("value") or "").strip().lower()
            val["selected_count"] = contact_val_counts[f].get(v, 0)

    # Step 4: store the refreshed filters (with selected_count + keyword list) and counts.
    doc_ref.update({
        "filters":              filters,
        "counts":               counts,
        "counts_generated_at":  datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    return counts


LEADS_COLLECTION_NAME = "leads"

# Leads pipeline scalar fields (facet name == leads field name)
LEADS_SCALAR_FIELDS = (
    "ai_sector", "ai_company_type", "ai_platform",
    "country", "ai_reseller_potential", "ai_client_base",
)
LEADS_ARRAY_FIELDS = ("ai_specialisation",)
# Contact fields from email_contacts (same names in both pipelines)
LEADS_CONTACT_FIELDS = ("title", "email_type")


def run_leads_filter_count(db, name: str) -> dict:
    """Count leads-pipeline contacts for filter_facets/<name> (pipeline=='leads')."""
    if not name:
        raise ValueError("filter-count job requires a 'name'")
    doc_ref = db.collection(FILTER_FACETS_COLLECTION).document(name)
    snap = doc_ref.get()
    if not snap.exists:
        raise ValueError(f"filter_facets/'{name}' not found")
    facet = snap.to_dict() or {}
    filters = facet.get("filters", {}) or {}

    lead_scalar = {f: _selected_values(filters[f])
                   for f in LEADS_SCALAR_FIELDS
                   if f in filters and _selected_values(filters[f])}
    contact_sel = {f: _selected_values(filters[f])
                   for f in LEADS_CONTACT_FIELDS
                   if f in filters and _selected_values(filters[f])}

    lead_array = {f: _selected_values(filters[f])
                  for f in LEADS_ARRAY_FIELDS
                  if f in filters and _selected_values(filters[f])}

    def lead_match(d: dict) -> bool:
        for f, sel in lead_scalar.items():
            if not _starts_any(d.get(f), sel):
                return False
        for f, sel in lead_array.items():
            have = [str(x).strip().lower() for x in (d.get(f) or [])]
            if not any(h.startswith(k) for h in have for k in sel):
                return False
        return True

    def contact_match(d: dict) -> bool:
        for f, sel in contact_sel.items():
            if not _starts_any(d.get(f), sel):
                return False
        return True

    # 1. Filter leads collection
    lead_fields = list(LEADS_SCALAR_FIELDS) + list(LEADS_ARRAY_FIELDS) + ["lead_id"]
    candidate_leads: set = set()
    lead_val_counts: dict = {f: Counter() for f in list(LEADS_SCALAR_FIELDS) + list(LEADS_ARRAY_FIELDS)}
    for d in db.collection(LEADS_COLLECTION_NAME).select(lead_fields).stream():
        data = d.to_dict() or {}
        if lead_match(data):
            candidate_leads.add(data.get("lead_id") or d.id)
            for f in LEADS_SCALAR_FIELDS:
                v = str(data.get(f) or "").strip().lower()
                if v:
                    lead_val_counts[f][v] += 1
            for f in LEADS_ARRAY_FIELDS:
                for item in _to_list(data.get(f)):
                    w = item.lower()
                    if w:
                        lead_val_counts[f][w] += 1

    # 2. Count email_contacts (mark_leads==True) that match candidates + contact filter
    n_contacts = 0
    n_in_campaigns = 0
    contact_val_counts: dict = {f: Counter() for f in LEADS_CONTACT_FIELDS}
    email_ids_in_campaigns: set = set()
    for d in db.collection_group("campaign_contacts").select(["email"]).stream():
        email_ids_in_campaigns.add(str((d.to_dict() or {}).get("email") or "").strip().lower())

    for d in db.collection(EMAIL_CONTACTS_COLLECTION).where(filter=FieldFilter("mark_leads", "==", True)).select(list(LEADS_CONTACT_FIELDS) + ["lead_id_leads", "email"]).stream():
        data = d.to_dict() or {}
        if data.get("lead_id_leads") not in candidate_leads:
            continue
        if contact_sel and not contact_match(data):
            continue
        n_contacts += 1
        email = str(data.get("email") or "").strip().lower()
        if email in email_ids_in_campaigns:
            n_in_campaigns += 1
        for f in LEADS_CONTACT_FIELDS:
            raw = str(data.get(f) or "").strip().lower()
            v = _first_word(raw) if f == "title" else raw
            if v:
                contact_val_counts[f][v] += 1

    counts = {
        "leads":                      len(candidate_leads),
        "contacts":                   n_contacts,
        "contacts_in_email_contacts": n_contacts,   # all are already in email_contacts
        "contacts_in_campaigns":      n_in_campaigns,
        "contacts_not_in_campaigns":  n_contacts - n_in_campaigns,
    }

    # Write selected_count onto facet values
    for f in list(LEADS_SCALAR_FIELDS) + list(LEADS_ARRAY_FIELDS):
        for val in (filters.get(f) or {}).get("values", []):
            v = str(val.get("value") or "").strip().lower()
            val["selected_count"] = lead_val_counts[f].get(v, 0)
    for f in LEADS_CONTACT_FIELDS:
        for val in (filters.get(f) or {}).get("values", []):
            v = str(val.get("value") or "").strip().lower()
            val["selected_count"] = contact_val_counts[f].get(v, 0)

    doc_ref.update({
        "filters":             filters,
        "counts":              counts,
        "counts_generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    return counts
