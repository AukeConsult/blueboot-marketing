"""facet_campaign_lib.py -- Create a campaign from a saved filter-facets preset.

Algorithm
---------
1. Load filter_facets/{facet_name} from Firestore.
2. Parse selected values (same OR-within / AND-across semantics as
   filter_count_lib.py).
3. Collect all emails already assigned to any OTHER campaign
   (collection_group query on campaign_contacts) — these are excluded.
4. Stream email_contacts; keep contacts that:
     a. match the filter, AND
     b. are NOT already in another campaign.
5. Create / overwrite the campaign document in campaigns/{campaign_id}.
6. Batch-write matching contacts to
   campaigns/{campaign_id}/campaign_contacts/{doc_id}.
7. Return a summary dict.

Field mapping (filter facet field → email_contacts field):
  platform        → ai_platform   (scraped CMS is stored as ai_platform in ec)
  ai_platform     → ai_platform
  ai_sector       → ai_sector
  ai_company_type → ai_company_type
  country         → country
  ai_country      → ai_country
  location        → location
  location_country→ location_country
  keywords        → keywords  (array)
  page_count      → page_count (size band)
  title           → title
  email_type      → email_type
  occupation      → (not stored on email_contacts; ignored gracefully)
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone

FILTER_FACETS_COLLECTION = "filter_facets"
CAMPAIGNS_COLLECTION = "campaigns"
CAMPAIGN_CONTACTS_SUB = "campaign_contacts"
EMAIL_CONTACTS_COLLECTION = "email_contacts"

BATCH_SIZE = 400

# (facet field name, email_contacts field name, kind)
# kind: "scalar" | "array" | "group" | "contact_scalar"
# "platform" and "ai_platform" both resolve to ec.ai_platform (OR'd together).
_FIELD_SPEC: list[tuple[str, str, str]] = [
    ("platform",        "ai_platform",      "scalar"),
    ("ai_platform",     "ai_platform",      "scalar"),
    ("ai_sector",       "ai_sector",        "scalar"),
    ("ai_company_type", "ai_company_type",  "scalar"),
    ("country",         "country",          "scalar"),
    ("ai_country",      "ai_country",       "scalar"),
    ("location",        "location",         "scalar"),
    ("location_country","location_country", "scalar"),
    ("keywords",        "keywords",         "array"),
    ("page_count",      "page_count",       "group"),
    ("title",           "title",            "contact_scalar"),
    ("email_type",      "email_type",       "contact_scalar"),
    # "occupation" not stored on email_contacts — omitted intentionally
]

# Canonical page bands (keep in sync with build_filter_facets.py)
_PAGE_BANDS = [
    ("micro",  1,      50),
    ("small",  51,     500),
    ("medium", 501,    3_000),
    ("large",  3_001,  10_000),
    ("huge",   10_001, 100_000),
    ("ultra",  100_001, None),
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
    return m.group(0).lower() if m else ""


def _starts_any(value: str, keys: set) -> bool:
    """True if lowercased value starts with any selected key (prefix match)."""
    v = str(value or "").strip().lower()
    return any(v.startswith(k) for k in keys)


def _selected_values(facet: dict) -> set[str]:
    return {str(x.get("value") or "").strip().lower()
            for x in (facet.get("values") or []) if x.get("selected")}


def _selected_groups(facet: dict) -> set[str]:
    return {str(g.get("key") or "").strip().lower()
            for g in (facet.get("groups") or []) if g.get("selected")}


class _Filter:
    """Parses a filter_facets document and tests email_contacts dicts."""

    def __init__(self, filters: dict) -> None:
        # Merge platform + ai_platform selections into one set against ec.ai_platform
        platform_vals = _selected_values(filters.get("platform") or {})
        ai_platform_vals = _selected_values(filters.get("ai_platform") or {})

        # scalar ec fields → selected values set
        self._scalar: dict[str, set[str]] = {}
        if platform_vals | ai_platform_vals:
            self._scalar["ai_platform"] = platform_vals | ai_platform_vals
        for fname, ecfield, kind in _FIELD_SPEC:
            if kind != "scalar" or fname in ("platform", "ai_platform"):
                continue
            sel = _selected_values(filters.get(fname) or {})
            if sel:
                # merge into ec field (multiple facet fields may share one ec field)
                self._scalar.setdefault(ecfield, set()).update(sel)

        # array ec fields
        self._array: dict[str, set[str]] = {}
        for fname, ecfield, kind in _FIELD_SPEC:
            if kind != "array":
                continue
            sel = _selected_values(filters.get(fname) or {})
            if sel:
                self._array[ecfield] = sel

        # page_count band keys
        self._page_keys: set[str] = _selected_groups(filters.get("page_count") or {})

        # contact-level scalar fields on email_contacts
        self._contact: dict[str, set[str]] = {}
        for fname, ecfield, kind in _FIELD_SPEC:
            if kind != "contact_scalar":
                continue
            sel = _selected_values(filters.get(fname) or {})
            if sel:
                self._contact[ecfield] = sel

    def matches(self, ec: dict) -> bool:
        """Return True if the email_contacts doc passes all active filters."""
        for ecfield, sel in self._scalar.items():
            if not _starts_any(ec.get(ecfield), sel):
                return False
        for ecfield, sel in self._array.items():
            have = [str(v).strip().lower() for v in (ec.get(ecfield) or [])]
            if not any(h.startswith(k) for h in have for k in sel):
                return False
        if self._page_keys:
            if _page_key(ec.get("page_count")) not in self._page_keys:
                return False
        for ecfield, sel in self._contact.items():
            raw = str(ec.get(ecfield) or "").strip().lower()
            # title uses prefix/first-word match; others use starts_any
            val = _first_word(raw) if ecfield == "title" else raw
            if not any(val.startswith(k) for k in sel):
                return False
        return True

    @property
    def has_any(self) -> bool:
        return bool(self._scalar or self._array or self._page_keys or self._contact)


def _collect_existing_campaign_emails(
    db, exclude_campaign_id: str
) -> set[str]:
    """Return the set of lowercase emails already present in any campaign
    OTHER than exclude_campaign_id.

    Path structure: campaigns/{campaign_id}/campaign_contacts/{doc_id}
    We extract the owner campaign from path segment [1].
    Prints a per-campaign breakdown so the caller can verify the query.
    """
    from collections import Counter as _Counter
    taken: set[str] = set()
    per_campaign: _Counter = _Counter()

    for doc in db.collection_group(CAMPAIGN_CONTACTS_SUB).select(["email"]).stream():
        # campaigns/{campaign_id}/campaign_contacts/{doc_id}
        path_parts = doc.reference.path.split("/")
        owner_campaign = path_parts[1] if len(path_parts) >= 4 else ""
        if owner_campaign == exclude_campaign_id:
            continue
        email = str((doc.to_dict() or {}).get("email") or "").strip().lower()
        if email:
            taken.add(email)
            per_campaign[owner_campaign] += 1

    if per_campaign:
        print("[facet-campaign] dedup breakdown by campaign:", flush=True)
        for camp, n in per_campaign.most_common():
            print(f"  {camp}: {n} contacts", flush=True)
    else:
        print("[facet-campaign] no existing campaign_contacts found "
              f"(excluding '{exclude_campaign_id}')", flush=True)

    return taken


def run_facet_campaign(
    db,
    facet_name: str,
    campaign_id: str,
    dry_run: bool = False,
) -> dict:
    """Build a campaign from a saved filter-facets preset.

    Args:
        db:          Firestore client.
        facet_name:  Name of the filter_facets document to use as the source
                     filter (e.g. "site_leads" or a saved preset name).
        campaign_id: Target campaign document ID. Created if not found;
                     contact_count / sites_count are updated if it exists.
        dry_run:     If True, compute everything but write nothing.

    Returns a summary dict with keys: campaign_id, facet_name,
    contacts_matched, contacts_skipped_dedup, contacts_written,
    sites_count, countries, dry_run.
    """
    if not facet_name:
        raise ValueError("facet_name is required")
    if not campaign_id:
        raise ValueError("campaign_id is required")

    # ── 1. Load filter preset ────────────────────────────────────────────────
    snap = db.collection(FILTER_FACETS_COLLECTION).document(facet_name).get()
    if not snap.exists:
        raise ValueError(f"filter_facets/'{facet_name}' not found")
    filters: dict = (snap.to_dict() or {}).get("filters") or {}
    flt = _Filter(filters)

    print(f"[facet-campaign] facet='{facet_name}'  campaign='{campaign_id}'  "
          f"dry_run={dry_run}", flush=True)
    print(f"[facet-campaign] active filter fields: "
          f"scalar={list(flt._scalar)}, array={list(flt._array)}, "
          f"page_keys={flt._page_keys}, contact={list(flt._contact)}", flush=True)

    # ── 2. Collect emails already in other campaigns (dedup set) ─────────────
    print("[facet-campaign] loading existing campaign contacts for dedup…", flush=True)
    taken_emails = _collect_existing_campaign_emails(db, campaign_id)
    print(f"[facet-campaign] {len(taken_emails)} emails already in other campaigns",
          flush=True)

    # ── 3. Stream email_contacts, apply filter + dedup ───────────────────────
    print("[facet-campaign] streaming email_contacts…", flush=True)
    matched: list[dict] = []
    skipped_dedup = 0
    skipped_filter = 0

    for doc in db.collection(EMAIL_CONTACTS_COLLECTION).stream():
        ec = doc.to_dict() or {}
        if not flt.has_any or flt.matches(ec):
            email = str(ec.get("email") or "").strip().lower()
            if not email:
                skipped_filter += 1
                continue
            if email in taken_emails:
                skipped_dedup += 1
                continue
            # Ensure doc_id is always set
            ec.setdefault("doc_id", doc.id)
            matched.append(ec)
        else:
            skipped_filter += 1

    print(f"[facet-campaign] matched={len(matched)}  "
          f"skipped_filter={skipped_filter}  skipped_dedup={skipped_dedup}", flush=True)

    # ── 4. Compute campaign-level stats ─────────────────────────────────────
    sites: set[str] = set()
    country_counter: Counter = Counter()
    for ec in matched:
        lid = ec.get("lead_id_site") or ec.get("lead_id_leads") or ""
        if lid:
            sites.add(lid)
        c = str(ec.get("country") or ec.get("location_country") or "").strip()
        if c:
            country_counter[c] += 1
    countries_list = [c for c, _ in country_counter.most_common()]

    if dry_run:
        print(f"[facet-campaign] DRY RUN — no writes.", flush=True)
        return {
            "campaign_id":               campaign_id,
            "facet_name":                facet_name,
            "emails_in_other_campaigns": len(taken_emails),
            "contacts_matched":          len(matched),
            "contacts_skipped_dedup":    skipped_dedup,
            "contacts_added":            None,   # unknown without reading existing
            "contacts_refreshed":        None,
            "contacts_removed":          None,
            "contacts_protected":        None,
            "dedup_by_campaign":         dedup_by_campaign,
            "sites_count":               len(sites),
            "countries":                 countries_list,
            "dry_run":                   True,
        }

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # ── 5a. Build facet reference fields ────────────────────────────────────
    facet_filters_snapshot: dict = {}
    for field, vals in flt._scalar.items():
        facet_filters_snapshot[field] = sorted(vals)
    for field, vals in flt._array.items():
        facet_filters_snapshot[field] = sorted(vals)
    if flt._page_keys:
        facet_filters_snapshot["page_count"] = sorted(flt._page_keys)
    for field, vals in flt._contact.items():
        facet_filters_snapshot[field] = sorted(vals)

    facet_ref_fields = {
        "source_facet":         facet_name,
        "source_facet_path":    f"{FILTER_FACETS_COLLECTION}/{facet_name}",
        "source_facet_filters": facet_filters_snapshot,
        "source_facet_built_at": now,
    }

    # ── 5b. Create / update campaign document ─────────────────────────────────
    camp_ref = db.collection(CAMPAIGNS_COLLECTION).document(campaign_id)
    camp_snap = camp_ref.get()
    if camp_snap.exists:
        camp_ref.update({
            "contact_count": len(matched),
            "sites_count":   len(sites),
            "countries":     countries_list,
            **facet_ref_fields,
            "updated_at":    now,
        })
        print(f"[facet-campaign] updated existing campaign '{campaign_id}'", flush=True)
    else:
        camp_ref.set({
            "campaign_id":            campaign_id,
            "status":                 "draft",
            "sent_at":                None,
            "outreach_email_account": "",
            "mail":                   {"subject": "", "body": "", "type": "plain"},
            "contact_count":          len(matched),
            "sites_count":            len(sites),
            "countries":              countries_list,
            **facet_ref_fields,
            "status_breakdown":       {},
            "select_breakdown":       {},
            "tier_breakdown":         {},
            "outreach_breakdown":     {},
            "updated_at":             now,
        })
        print(f"[facet-campaign] created new campaign '{campaign_id}'", flush=True)

    # ── 6. Load existing campaign_contacts (for rerun awareness) ────────────
    contacts_col = camp_ref.collection(CAMPAIGN_CONTACTS_SUB)
    # doc_id -> {status, ...lifecycle fields}
    existing: dict[str, dict] = {}
    for doc in contacts_col.select(
            ["status", "sent_at", "last_action", "last_action_status"]).stream():
        existing[doc.id] = doc.to_dict() or {}
    print(f"[facet-campaign] existing contacts in campaign: {len(existing)}", flush=True)

    matched_ids: set[str] = set()

    # ── 7. Batch-write matched contacts ──────────────────────────────────────
    # Lifecycle fields are preserved for contacts that already exist.
    # New contacts get status=pending and blank lifecycle fields.
    LIFECYCLE = ("status", "sent_at", "last_action", "last_action_status")
    added = refreshed = 0
    for i in range(0, len(matched), BATCH_SIZE):
        chunk = matched[i:i + BATCH_SIZE]
        batch = db.batch()
        for ec in chunk:
            doc_id = ec.get("doc_id") or re.sub(
                r"[^a-zA-Z0-9_-]", "_",
                str(ec.get("email") or "").strip().lower()
            )
            matched_ids.add(doc_id)
            lead_id = ec.get("lead_id_site") or ec.get("lead_id_leads") or ""
            contact_doc = {
                "doc_id":        doc_id,
                "email":         ec.get("email", ""),
                "name":          ec.get("name", ""),
                "title":         ec.get("title", ""),
                "website":       ec.get("website", ""),
                "lead_id":       lead_id,
                "source_facet":  facet_name,
                "added_at":      now,
            }
            if doc_id in existing:
                # Preserve all lifecycle fields — never overwrite outreach history
                prev = existing[doc_id]
                for field in LIFECYCLE:
                    contact_doc[field] = prev.get(field, "" if field != "sent_at" else None)
                batch.set(contacts_col.document(doc_id), contact_doc, merge=True)
                refreshed += 1
            else:
                # New contact — set initial lifecycle values
                contact_doc.update({
                    "status":             "pending",
                    "sent_at":            None,
                    "last_action":        "",
                    "last_action_status": "",
                })
                batch.set(contacts_col.document(doc_id), contact_doc, merge=False)
                added += 1
        batch.commit()
        print(f"[facet-campaign]   written {added + refreshed}/{len(matched)} "
              f"(+{added} new, ~{refreshed} refreshed)", flush=True)

    # ── 8. Remove stale pending contacts ─────────────────────────────────────
    # Only delete contacts that are still pending — any other status means
    # outreach has started and the record must be kept.
    stale_ids = [
        doc_id for doc_id, data in existing.items()
        if doc_id not in matched_ids
        and (data.get("status") or "pending") == "pending"
    ]
    removed = 0
    if stale_ids:
        print(f"[facet-campaign] removing {len(stale_ids)} stale pending contacts",
              flush=True)
        for i in range(0, len(stale_ids), BATCH_SIZE):
            batch = db.batch()
            for doc_id in stale_ids[i:i + BATCH_SIZE]:
                batch.delete(contacts_col.document(doc_id))
            batch.commit()
            removed += len(stale_ids[i:i + BATCH_SIZE])

    protected = len(existing) - len(matched_ids & set(existing)) - len(stale_ids)
    if protected > 0:
        print(f"[facet-campaign] {protected} contacts kept (non-pending, no longer match filter)",
              flush=True)

    written = added + refreshed
    print(f"[facet-campaign] done. {written} contacts in campaign '{campaign_id}' "
          f"(+{added} new, ~{refreshed} refreshed, -{removed} removed)", flush=True)
    return {
        "campaign_id":               campaign_id,
        "facet_name":                facet_name,
        "emails_in_other_campaigns": len(taken_emails),
        "contacts_matched":          len(matched),
        "contacts_skipped_dedup":    skipped_dedup,
        "contacts_added":            added,
        "contacts_refreshed":        refreshed,
        "contacts_removed":          removed,
        "contacts_protected":        protected,
        "sites_count":               len(sites),
        "countries":                 countries_list,
        "dry_run":                   False,
    }
