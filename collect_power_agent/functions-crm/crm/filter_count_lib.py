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

import re
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

# (key, lo, hi) -- canonical page bands, kept in sync with build_filter_facets.py
_PAGE_BANDS = [
    ("micro", 1, 50), ("small", 51, 500), ("medium", 501, 3000),
    ("large", 3001, 10000), ("huge", 10001, 100000), ("ultra", 100001, None),
]


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


def run_filter_count(db, name: str) -> dict:
    """Count matching leads/contacts for filter_facets/<name> and store results."""
    if not name:
        raise ValueError("filter-count job requires a 'name'")
    doc_ref = db.collection(FILTER_FACETS_COLLECTION).document(name)
    snap = doc_ref.get()
    if not snap.exists:
        raise ValueError(f"filter_facets/'{name}' not found")
    filters = (snap.to_dict() or {}).get("filters", {}) or {}

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
            have = [str(x).strip().lower() for x in (d.get(f) or [])]
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
    for d in db.collection(LEADS_COLLECTION).select(lead_fields).stream():
        data = d.to_dict() or {}
        if lead_match(data):
            candidate_leads.add(data.get("lead_id") or d.id)

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

    counts = {
        "leads":                          len(matched_sites),
        "contacts":                       n_contacts,
        "lead_candidates":                len(candidate_leads),
        "contacts_in_email_contacts":     len(seen_in),
        "contacts_not_in_email_contacts": len(seen_not),
    }
    doc_ref.update({
        "counts":              counts,
        "counts_generated_at": datetime.now(timezone.utc).isoformat(),
    })
    return counts
