"""filter_site_leads.py -- Filter site_leads (+ their site_contacts) using the
selectable values catalogued by build_filter_facets.py.

Filter semantics:
  * within one field  -> OR  (a lead matches if it equals ANY selected value)
  * across fields     -> AND (a lead must satisfy EVERY provided field)
  * page_count        -> list of size-band keys (micro/small/medium/large/huge/ultra
                         /unknown from PAGE_GROUPS); matches if page_count is in ANY band
  * keywords          -> array membership (matches if ANY selected keyword is present)
  * contact fields    -> occupation / title / email_type live on site_contacts. A lead
                         matches if it has >=1 contact satisfying ALL contact filters;
                         the matching contacts are returned with the lead.

Both collections are streamed once and filtered in memory. This deliberately avoids
Firestore composite indexes: an arbitrary combination of equalities plus a page_count
range would otherwise need a dedicated composite index per combination (and raise
FAILED_PRECONDITION until created). For a few thousand docs an in-memory pass is fast
and can never hit that error.

Importable:
    from app.filter_site_leads import filter_leads
    res = filter_leads({"ai_sector": ["technology"], "page_count": ["large", "huge"],
                        "email_type": ["personal"]})

CLI:
    python app/filter_site_leads.py --filter ai_sector=technology,ecommerce \
                                    --filter country=NO --filter page_count=large,huge \
                                    --filter email_type=personal --limit 50
    python app/filter_site_leads.py --filters-json '{"platform":["woocommerce"]}'

Synchronous, single-threaded read script (no asyncio) -- no wait_for/locks needed.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import _pathsetup  # noqa: F401  -- sets Windows selector loop / sys.path

try:
    from app.build_filter_facets import PAGE_GROUPS, _page_group_key
except ImportError:
    from build_filter_facets import PAGE_GROUPS, _page_group_key

COLLECTION_DEFAULT = "site_leads"
CONTACTS_SUBCOLLECTION = "site_contacts"
EMAIL_CONTACTS_COLLECTION = "email_contacts"
OUTPUT_DIR = Path(__file__).parent.parent / "output"

# Which field lives where / how it is matched.
LEAD_SCALAR_FIELDS = (
    "platform", "ai_platform", "ai_sector", "ai_company_type",
    "country", "ai_country", "location", "location_country",
)
LEAD_ARRAY_FIELDS = ("keywords",)
CONTACT_FIELDS = ("occupation", "title", "email_type")
GROUP_FIELD = "page_count"
IN_EMAIL_CONTACTS_FIELD = "in_email_contacts"
VALID_PAGE_KEYS = {k for k, *_ in PAGE_GROUPS} | {"unknown"}

# Contact fields we return alongside each matched lead.
CONTACT_OUT_FIELDS = (
    "contact_id", "email", "name", "title", "occupation",
    "email_type", "country", "ai_country", "phone",
)


def _email_contacts_doc_id(email) -> str:
    """email_contacts doc-id: lowercased email, non-alnum chars -> "_"
    (must match app/site_smart_export.py._doc_id)."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", str(email or "").strip().lower())


def _starts_any(value, keys) -> bool:
    """True if the (lowercased) real value starts with ANY selected key.
    Prefix match: key 'daglig' matches real value 'daglig leder'."""
    v = str(value or "").strip().lower()
    return any(v.startswith(k) for k in keys)


def _norm_set(values) -> set[str]:
    """Lowercased, stripped set for case-insensitive membership tests."""
    out: set[str] = set()
    for v in (values or []):
        s = str(v).strip().lower()
        if s:
            out.add(s)
    return out


def _get_db():
    try:
        from app.firestore_client import get_firestore
    except ImportError:
        from firestore_client import get_firestore
    return get_firestore()


class LeadFilter:
    """Isolated, never-raises filter over site_leads + site_contacts.

    Owns its own parsed-filter state; build once, call run() once.
    """

    def __init__(self, filters: dict, collection: str = COLLECTION_DEFAULT,
                 limit: int | None = None, with_contacts: bool = True) -> None:
        self.collection = collection
        self.limit = limit
        self.with_contacts = with_contacts
        self.warnings: list[str] = []

        filters = filters or {}
        self.lead_scalar = {f: _norm_set(filters[f])
                            for f in LEAD_SCALAR_FIELDS if filters.get(f)}
        self.lead_array = {f: _norm_set(filters[f])
                           for f in LEAD_ARRAY_FIELDS if filters.get(f)}
        self.contact = {f: _norm_set(filters[f])
                        for f in CONTACT_FIELDS if filters.get(f)}

        # Optional condition: contact present in the email_contacts collection.
        # None = no condition, True = must exist, False = must NOT exist.
        self.in_email_contacts = None
        if filters.get(IN_EMAIL_CONTACTS_FIELD):
            raw = str(filters[IN_EMAIL_CONTACTS_FIELD][0]).strip().lower()
            if raw in ('yes', 'true', '1', 'in', 'exists'):
                self.in_email_contacts = True
            elif raw in ('no', 'false', '0', 'not', 'missing'):
                self.in_email_contacts = False
            else:
                self.warnings.append(
                    f"ignored unknown {IN_EMAIL_CONTACTS_FIELD} value: {raw!r}")

        self.page_keys: set[str] = set()
        if filters.get(GROUP_FIELD):
            for k in filters[GROUP_FIELD]:
                kk = str(k).strip().lower()
                if kk in VALID_PAGE_KEYS:
                    self.page_keys.add(kk)
                else:
                    self.warnings.append(f"ignored unknown page_count band: {k!r}")

        known = (set(LEAD_SCALAR_FIELDS) | set(LEAD_ARRAY_FIELDS)
                 | set(CONTACT_FIELDS) | {GROUP_FIELD, IN_EMAIL_CONTACTS_FIELD})
        for f in filters:
            if f not in known:
                self.warnings.append(f"ignored unknown filter field: {f!r}")

    # -- matching helpers --------------------------------------------------
    def _lead_matches(self, data: dict) -> bool:
        for field, selected in self.lead_scalar.items():
            if not _starts_any(data.get(field), selected):
                return False
        for field, selected in self.lead_array.items():
            have = _norm_set(data.get(field))
            if not any(h.startswith(k) for h in have for k in selected):
                return False
        if self.page_keys:
            if _page_group_key(data.get("page_count")) not in self.page_keys:
                return False
        return True

    def _contact_matches(self, data: dict) -> bool:
        for field, selected in self.contact.items():
            if not _starts_any(data.get(field), selected):
                return False
        return True

    def _load_email_contacts_ids(self, db) -> set:
        """All email_contacts doc IDs (sanitized emails). Never raises."""
        ids: set[str] = set()
        try:
            for doc in db.collection(EMAIL_CONTACTS_COLLECTION).select([]).stream():
                ids.add(doc.id)
        except Exception as exc:
            self.warnings.append(f"could not load {EMAIL_CONTACTS_COLLECTION}: {exc}")
        return ids

    # -- main --------------------------------------------------------------
    def run(self) -> dict:
        db = _get_db()

        # 1. Lead-level pass.
        lead_fields = list(LEAD_SCALAR_FIELDS) + list(LEAD_ARRAY_FIELDS) + [
            GROUP_FIELD, "domain", "website", "company", "title", "lead_id",
        ]
        matched: dict[str, dict] = {}
        for doc in db.collection(self.collection).select(lead_fields).stream():
            data = doc.to_dict() or {}
            if self._lead_matches(data):
                data["lead_id"] = data.get("lead_id") or doc.id
                matched[doc.id] = data

        # 2. Contact pass (only if we need contacts or filter on them).
        has_contact_cond = bool(self.contact) or self.in_email_contacts is not None
        need_contacts = self.with_contacts or has_contact_cond
        contacts_by_lead: dict[str, list[dict]] = {}
        # Existence check only -- dedupe by sanitized email so the same address
        # is never counted twice across contacts/leads.
        seen_in: set[str] = set()
        seen_not_in: set[str] = set()
        if need_contacts:
            email_ids = self._load_email_contacts_ids(db)
            c_fields = list(CONTACT_OUT_FIELDS) + ["lead_id"]
            for doc in db.collection_group(CONTACTS_SUBCOLLECTION).select(c_fields).stream():
                data = doc.to_dict() or {}
                lid = data.get("lead_id")
                if not lid or lid not in matched:
                    continue
                if self.contact and not self._contact_matches(data):
                    continue
                cid = _email_contacts_doc_id(data.get("email"))
                exists = cid in email_ids
                if self.in_email_contacts is not None and exists != self.in_email_contacts:
                    continue
                (seen_in if exists else seen_not_in).add(cid)
                out = {k: data.get(k) for k in CONTACT_OUT_FIELDS}
                out["in_email_contacts"] = exists
                contacts_by_lead.setdefault(lid, []).append(out)

        # 3. Assemble. If a contact condition is set, keep only leads with >=1 match.
        out_leads: list[dict] = []
        for lid, lead in matched.items():
            kids = contacts_by_lead.get(lid, [])
            if has_contact_cond and not kids:
                continue
            if need_contacts:
                lead = {**lead, "matching_contacts": kids,
                        "matching_contact_count": len(kids),
                        "in_email_contacts_count":
                            sum(1 for c in kids if c.get("in_email_contacts"))}
            out_leads.append(lead)
            if self.limit and len(out_leads) >= self.limit:
                break

        out_leads.sort(key=lambda d: int(d.get("page_count") or 0), reverse=True)
        return {
            "collection": self.collection,
            "filters_applied": {
                **{f: sorted(v) for f, v in self.lead_scalar.items()},
                **{f: sorted(v) for f, v in self.lead_array.items()},
                **({GROUP_FIELD: sorted(self.page_keys)} if self.page_keys else {}),
                **{f: sorted(v) for f, v in self.contact.items()},
                **({IN_EMAIL_CONTACTS_FIELD: self.in_email_contacts}
                   if self.in_email_contacts is not None else {}),
            },
            "warnings": self.warnings,
            "matched_leads": len(out_leads),
            "email_contacts_summary": {
                "in_email_contacts": len(seen_in),
                "not_in_email_contacts": len(seen_not_in),
            },
            "leads": out_leads,
        }


def filter_leads(filters: dict, collection: str = COLLECTION_DEFAULT,
                 limit: int | None = None, with_contacts: bool = True) -> dict:
    """Convenience wrapper around LeadFilter."""
    return LeadFilter(filters, collection=collection, limit=limit,
                      with_contacts=with_contacts).run()


def _parse_filter_args(pairs: list[str]) -> dict:
    """Turn ['ai_sector=technology,ecommerce', 'country=NO'] into a filters dict."""
    filters: dict[str, list[str]] = {}
    for pair in pairs or []:
        if "=" not in pair:
            continue
        field, raw = pair.split("=", 1)
        vals = [v.strip() for v in raw.split(",") if v.strip()]
        if field.strip() and vals:
            filters.setdefault(field.strip(), []).extend(vals)
    return filters


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Filter site_leads (+ site_contacts) by the catalogued facet values.")
    ap.add_argument("--filter", action="append", default=[], metavar="field=v1,v2",
                    help="Repeatable. e.g. --filter ai_sector=technology,ecommerce")
    ap.add_argument("--filters-json", default="",
                    help='JSON object of filters, e.g. {"platform":["woocommerce"]}')
    ap.add_argument("--collection", default=COLLECTION_DEFAULT)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-contacts", action="store_true",
                    help="Do not attach contacts (faster when not filtering on them)")
    ap.add_argument("--out", default="", help="Optional path to write the result JSON")
    args = ap.parse_args()

    filters = _parse_filter_args(args.filter)
    if args.filters_json:
        try:
            filters.update(json.loads(args.filters_json))
        except json.JSONDecodeError as exc:
            ap.error(f"--filters-json is not valid JSON: {exc}")

    res = filter_leads(filters, collection=args.collection, limit=args.limit,
                       with_contacts=not args.no_contacts)

    for w in res["warnings"]:
        print(f"  [filter] WARNING: {w}")
    print(f"[filter] {res['matched_leads']} leads match {res['filters_applied']}")
    for lead in res["leads"][:20]:
        kids = lead.get("matching_contact_count", "")
        kids = f"  contacts={kids}" if kids != "" else ""
        print(f"  {str(lead.get('domain') or '')[:38]:38s}  "
              f"pages={lead.get('page_count', 0)}{kids}")

    out_path = Path(args.out) if args.out else (
        OUTPUT_DIR / f"filter_result_{args.collection}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[filter] wrote {out_path}")


if __name__ == "__main__":
    main()
