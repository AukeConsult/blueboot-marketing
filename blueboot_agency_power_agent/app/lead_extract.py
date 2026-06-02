"""extract_leads — filter leads from Firestore and export to Excel.

Reads lead documents (and their contacts sub-collections) directly from
the Firestore 'leads' collection.  No local CSV is required.

Usage (CLI):
    python extract_leads.py [options]

Options:
    --collection NAME   Firestore collection name            (default: leads /
                        FIRESTORE_COLLECTION env var)
    --output DIR        Directory to write the Excel file    (default: ../output)
    --min-score INT     Minimum reseller_score to include    (default: 0)
    --max-score INT     Maximum reseller_score to include    (default: 100)
    --country CODE      One or more country codes            (repeatable)
    --source MODE       search | catalog | both              (see below)
    --query TEXT        Substring match on source_query      (case-insensitive)
    --priority P        One or more priority labels          (repeatable)
    --with-email        Only leads that have ≥1 contact email
    --out FILE          Output filename                      (default: auto)

Source filter values:
    search   → found_by_search  == "yes"
    catalog  → found_by_catalog == "yes"
    both     → found by BOTH search AND catalog

Function API:
    from extract_leads import extract_leads

    path = extract_leads(
        output_dir="output",
        min_score=70,
        countries=["NO", "SE"],
        source="search",
        query="webbyrå",
        priorities=["A", "B"],
        with_email=True,
        out_file="my_extract.xlsx",
    )
"""
from __future__ import annotations

import threading as _threading
import re
import importlib.util
import os
from datetime import datetime
from pathlib import Path

import pandas as pd

import _pathsetup  # noqa: F401 — adds project root, app/, app/functions/, app/collect-functions/ to sys.path

try:
    from app.functions.utils import clean_contact_name as _clean_contact_name
except ModuleNotFoundError:
    from functions.utils import clean_contact_name as _clean_contact_name


# ---------------------------------------------------------------------------
# Firebase helpers
# ---------------------------------------------------------------------------

def _get_credentials():
    """Return a firebase_admin Certificate, or None if unavailable."""
    try:
        import firebase_admin.credentials as fb_creds
    except ImportError:
        print("  [firebase] firebase-admin not installed — run: pip install firebase-admin")
        return None

    # 1. blueboot_secrets.py in project root
    secrets_path = Path(__file__).parent.parent / "blueboot_secrets.py"
    if secrets_path.exists():
        try:
            spec = importlib.util.spec_from_file_location("blueboot_secrets", secrets_path)
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            key_dict = getattr(mod, "fireBaseAdminKey", None)
            if key_dict:
                return fb_creds.Certificate(key_dict)
        except Exception as e:
            print(f"  [firebase] could not load blueboot_secrets: {e}")

    # 2. JSON file fallback
    creds_path = os.getenv("FIREBASE_CREDENTIALS", "config/serviceAccountKey.json")
    if Path(creds_path).exists():
        return fb_creds.Certificate(creds_path)

    print("  [firebase] no credentials found.")
    return None


def _firestore_client(collection: str | None = None):
    """Return (db, col) — initialises Firebase lazily."""
    try:
        import firebase_admin
        from firebase_admin import firestore
    except ImportError:
        return None, None

    cred = _get_credentials()
    if cred is None:
        return None, None

    col_name = collection or os.getenv("FIRESTORE_COLLECTION", "leads")
    with _local_fb_lock:
        with _local_fb_lock:
            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred)

    db  = firestore.client()
    col = db.collection(col_name)
    return db, col


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

_KEYWORD_SEARCH_FIELDS = [
    "source_query", "keywords", "title", "description",
    "company", "domain", "website", "reasons",
]

def extract_leads(
    output_dir: str | Path | None = None,
    min_score: int = 0,
    max_score: int = 100,
    countries: list[str] | None = None,
    source: str | None = None,
    query: str | None = None,
    priorities: list[str] | None = None,
    ai_potentials: list[str] | None = None,
    with_email: bool = False,
    out_file: str | None = None,
    collection: str | None = None,
    keywords: list[str] | None = None,
    save_extract: str | None = None,
    extract_dry_run: bool = False,
    allow_reextract: bool = False,
    limit: int | None = None,
) -> Path | None:
    """Filter leads from Firestore and write a focused Excel extract.

    Parameters
    ----------
    output_dir      : directory to write the output Excel file
    min_score       : minimum reseller_score (inclusive)
    max_score       : maximum reseller_score (inclusive)
    countries       : list of country codes, e.g. ["NO", "SE"] — None = all
    source          : "search"  → found_by_search == "yes"
                      "catalog" → found_by_catalog == "yes"
                      "both"    → found by BOTH modes
                      None      → no source filter
    query           : substring match on source_query (case-insensitive)
    priorities      : list of priority labels, e.g. ["A", "B"] — None = all
    with_email      : if True, only include leads with ≥1 contact email
    out_file        : filename for the output Excel file (placed in output_dir);
                      defaults to extract_leads_YYYYMMDD_HHMMSS.xlsx
    collection      : Firestore collection name (default: "leads" /
                      FIRESTORE_COLLECTION env var)
    keywords        : list of keywords (OR logic) — a lead matches if ANY keyword
                      appears in source_query, keywords, title, description,
                      company, domain, website, or reasons (case-insensitive)
    save_extract    : if set to a name, save this extract as a named document in
                      the ``leads_extract`` Firestore collection.  The name becomes
                      the document ID (spaces → underscores).  Each lead is written
                      to a ``leads_extracted`` sub-collection and each contact to
                      ``contacts_extracted`` under that lead.  A lead already claimed
                      by another extract is skipped.
                      If None (no name given), the save is treated as a dry run.
    extract_dry_run : when True (and save_extract is set), print what would be
                      saved without writing anything to Firestore.
    limit           : maximum number of leads to include (applied after all
                      other filters, before contact loading). None = no limit.

    Returns
    -------
    Path to the written Excel file.
    """
    # Default: <project_root>/output  (always relative to this file, not CWD)
    if output_dir is None:
        output_dir = Path(__file__).parent.parent / "output"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    db, col = _firestore_client(collection)
    if col is None:
        raise RuntimeError("Could not connect to Firestore — check credentials.")

    col_name = collection or os.getenv("FIRESTORE_COLLECTION", "leads")
    print(f"[extract_leads] Reading from Firestore collection '{col_name}' …")

    # ------------------------------------------------------------------
    # Guard: abort if the named extract already exists in Firestore
    # ------------------------------------------------------------------
    # Derive a clean extract filename early — used for the Excel name regardless
    # of whether the Firestore write is skipped later.
    _extract_file_stem: str | None = None
    if save_extract:
        _extract_file_stem = re.sub(r"[^a-zA-Z0-9_\-]", "_", save_extract).strip("_") or "extract"

    if save_extract and not extract_dry_run:
        existing = db.collection("leads_extract").document(_extract_file_stem).get()
        if existing.exists:
            if allow_reextract:
                print(f"[extract_leads] Extract '{_extract_file_stem}' already exists — overwriting (--allow-reextract).")
                # Delete existing subcollection docs so we start fresh
                for old_doc in db.collection("leads_extract").document(_extract_file_stem).collection("leads_extracted").stream():
                    old_doc.reference.delete()
                db.collection("leads_extract").document(_extract_file_stem).delete()
                print(f"[extract_leads] Old extract deleted — will recreate with fresh leads.")
            else:
                print(f"[extract_leads] Extract '{_extract_file_stem}' already exists — reading from Firestore and writing Excel only.")
                print(f"[extract_leads] Tip: use --allow-reextract to overwrite with fresh leads.")
                return _excel_from_existing_extract(
                    db=db,
                    extract_id=_extract_file_stem,
                    output_dir=output_dir,
                    out_file=out_file,
                )

    # ------------------------------------------------------------------
    # Pre-load already-extracted lead IDs via collectionGroup
    # Each document in any leads_extracted sub-collection has the lead_id
    # as its document ID.  Collecting them once avoids marking the main
    # leads collection.
    # ------------------------------------------------------------------
    already_extracted_ids: set[str] = set()
    already_extracted: list[dict] = []

    if save_extract and not allow_reextract:
        print("[extract_leads] Checking leads_extracted collectionGroup for already-extracted leads…")
        PROGRESS_EVERY = 100
        for xdoc in db.collection_group("leads_extracted").stream():
            already_extracted_ids.add(xdoc.id)
            if len(already_extracted_ids) % PROGRESS_EVERY == 0:
                print(f"[extract_leads] {len(already_extracted_ids)} already-extracted leads scanned…")
        print(f"[extract_leads] {len(already_extracted_ids)} lead(s) already in an extract.")
    elif save_extract and allow_reextract:
        print("[extract_leads] --allow-reextract: skipping already-extracted check — leads may appear in multiple extracts.")

    # ------------------------------------------------------------------
    # Load all lead documents
    # ------------------------------------------------------------------
    country_upper = [c.upper() for c in countries] if countries else None
    priority_upper = [p.upper() for p in priorities] if priorities else None

    # Quick diagnostic: show sample of leads with the requested country
    if country_upper:
        _diag = list(col.where("country", "in", list(country_upper)).limit(5).stream()) if len(country_upper) <= 10 else []
        print(f"[extract_leads] Quick check — found {len(_diag)} sample lead(s) with country in {country_upper}:")
        for _d in _diag:
            _dd = _d.to_dict() or {}
            print(f"  {_dd.get('domain','?'):40s}  country={_dd.get('country','?')}  score={_dd.get('reseller_score','?')}  priority={(_dd.get('priority') or '?')[:12]}")
        if not _diag:
            print("[extract_leads]  → 0 found — leads may be stored with a different country value")

    lead_rows: list[dict] = []
    skipped = 0
    _skip_reasons: dict[str, int] = {}   # filter breakdown for diagnostics
    _country_sample: list[str] = []

    def _skip(reason: str):
        nonlocal skipped
        skipped += 1
        _skip_reasons[reason] = _skip_reasons.get(reason, 0) + 1

    # Use Firestore country filter to avoid scanning all docs
    if country_upper and len(country_upper) <= 10:
        _stream = col.where("country", "in", list(country_upper)).stream()
        print(f"[extract_leads] Using Firestore query for country={list(country_upper)}", flush=True)
    else:
        _stream = col.stream()

    for doc in _stream:
        d = doc.to_dict()
        if not d:
            continue

        raw_country = (d.get("country") or "").strip()

        # --- global leads (country="*") are excluded from extract ---
        if raw_country == "*":
            _skip("global(*)")
            continue

        # --- country filter (also catches any that slipped through the query) ---
        raw_country_upper = raw_country.upper()
        if len(_country_sample) < 10 and raw_country_upper:
            _country_sample.append(raw_country_upper)
        if country_upper:
            if raw_country_upper not in country_upper:
                _skip(f"country({raw_country_upper})")
                continue

        # --- score ---
        score = 0
        try:
            score = int(float(d.get("reseller_score", 0) or 0))
        except (ValueError, TypeError):
            pass
        if score < min_score or score > max_score:
            _skip(f"score({score})<{min_score}" if score < min_score else f"score({score})>{max_score}")
            continue

        # --- already claimed by another extract (checked after country filter) ---
        if save_extract and already_extracted_ids:
            lid = d.get("lead_id") or d.get("domain", "")
            if lid and lid in already_extracted_ids:
                already_extracted.append({
                    "domain": d.get("domain", ""),
                    "lead_id": lid,
                })
                _skip("already_extracted")
                continue

        # --- source ---
        if source == "search":
            if (d.get("found_by_search") or "").lower() != "yes":
                _skip("source(not_search)")
                continue
        elif source == "catalog":
            if (d.get("found_by_catalog") or "").lower() != "yes":
                _skip("source(not_catalog)")
                continue
        elif source == "both":
            if not ((d.get("found_by_search") or "").lower() == "yes" and
                    (d.get("found_by_catalog") or "").lower() == "yes"):
                _skip("source(not_both)")
                continue

        # --- query substring ---
        if query:
            sq = (d.get("source_query") or "").lower()
            if query.lower() not in sq:
                _skip("query_mismatch")
                continue

        # --- priority ---
        if priority_upper:
            # priority field is like "A - High fit" — match on leading letter only
            lead_priority = (d.get("priority") or "").upper().strip()
            if not any(lead_priority == p or lead_priority.startswith(p + " ") or lead_priority.startswith(p + "-") for p in priority_upper):
                _skip(f"priority({lead_priority or 'none'})")
                continue

        # --- ai_reseller_potential ---
        if ai_potentials:
            pot = (d.get("ai_reseller_potential") or "").lower()
            if pot not in [p.lower() for p in ai_potentials]:
                _skip(f"ai_potential({pot or 'none'})")
                continue

        # --- keywords (OR logic across multiple fields) ---
        if keywords:
            haystack = " ".join(
                str(d.get(f) or "") for f in _KEYWORD_SEARCH_FIELDS
            ).lower()
            if not any(kw.lower() in haystack for kw in keywords):
                _skip("keyword_mismatch")
                continue

        lead_rows.append(d)
        if limit and len(lead_rows) >= limit:
            break

    if country_upper and _country_sample:
        print(f"[extract_leads] Sample 'country' values in Firestore: {sorted(set(_country_sample))}")
    print(f"[extract_leads] {len(lead_rows)} leads matched, {skipped} filtered out")
    if _skip_reasons:
        # Only print filters that are meaningful — skip the country/global noise
        # when a country filter is active (those are expected and high-volume)
        meaningful = {k: v for k, v in _skip_reasons.items()
                      if not k.startswith("country(") and k != "global(*)"}
        country_skipped = sum(v for k, v in _skip_reasons.items()
                              if k.startswith("country(") or k == "global(*)")
        if country_skipped:
            print(f"[extract_leads]   {country_skipped:>6}  other countries / global (filtered by --country)")
        if meaningful:
            print("[extract_leads] Filter breakdown:")
            for reason, count in sorted(meaningful.items(), key=lambda x: -x[1]):
                print(f"[extract_leads]   {count:>6}  {reason}")
    if already_extracted:
        print(f"[extract_leads] {len(already_extracted)} lead(s) skipped — already in another extract:")
        for ae in already_extracted[:20]:
            print(f"  {ae['domain']}")
        if len(already_extracted) > 20:
            print(f"  … and {len(already_extracted) - 20} more")

    # ------------------------------------------------------------------
    # Load contacts sub-collections for matched leads
    # Only contacts that carry a real email address are kept.
    # ------------------------------------------------------------------
    contact_rows: list[dict] = []
    leads_with_email: set[str] = set()

    for lead in lead_rows:
        lid = lead.get("lead_id") or lead.get("domain", "")
        if not lid:
            continue
        for cdoc in col.document(lid).collection("contacts").stream():
            c = cdoc.to_dict()
            if not c:
                continue
            # Only include contacts that have a non-empty, well-formed email address.
            # Reject unicode-escape artifacts such as "u003ehector@..." that
            # occur when \uXXXX sequences lose their backslash during scraping.
            email_val = (c.get("email") or "").strip()
            if not email_val:
                continue
            local = email_val.split("@", 1)[0]
            if re.search(r'u00[0-9a-f]{2}', local, re.IGNORECASE):
                continue
            if not re.fullmatch(r'[a-zA-Z0-9_.+\-]+@[a-zA-Z0-9\-]+(?:\.[a-zA-Z0-9\-]+)+',
                                email_val):
                continue
            # Validate name against email — clear it if it looks wrong
            c = dict(c)  # don't mutate the Firestore object
            c["name"] = _clean_contact_name(c.get("name", ""), email_val)
            contact_rows.append(c)
            leads_with_email.add(lid)

    print(f"[extract_leads] {len(contact_rows)} contacts with email loaded "
          f"across {len(leads_with_email)} leads")

    # ------------------------------------------------------------------
    # Apply with_email filter (post contact-load)
    # ------------------------------------------------------------------
    if with_email:
        before = len(lead_rows)
        lead_rows = [
            r for r in lead_rows
            if (r.get("lead_id") or r.get("domain", "")) in leads_with_email
        ]
        # contacts are already email-only; re-filter to matched leads
        kept_lids = {r.get("lead_id") or r.get("domain", "") for r in lead_rows}
        contact_rows = [c for c in contact_rows if (c.get("lead_id") or "") in kept_lids]
        print(f"[extract_leads] --with-email: {before - len(lead_rows)} leads removed, "
              f"{len(lead_rows)} remain")

    # ------------------------------------------------------------------
    # Build DataFrames
    # ------------------------------------------------------------------
    leads_df = pd.DataFrame(lead_rows) if lead_rows else pd.DataFrame()

    # Drop internal scraping artefact columns from the Leads sheet too
    _LEADS_DROP = {"email_phones", "email_names"}
    if not leads_df.empty:
        leads_df.drop(columns=[c for c in _LEADS_DROP if c in leads_df.columns], inplace=True)

    # Normalise reseller_score to int
    if not leads_df.empty and "reseller_score" in leads_df.columns:
        leads_df["reseller_score"] = pd.to_numeric(
            leads_df["reseller_score"], errors="coerce"
        ).fillna(0).astype(int)

    # ------------------------------------------------------------------
    # Flat "one row per email" sheet
    # Each contact row gets all lead fields merged in.
    # Column order: contact fields first, then lead-only fields.
    # Leads without any email contact appear at the bottom with empty
    # contact fields so no lead data is lost.
    # ------------------------------------------------------------------
    CONTACT_COLS = ["email", "name", "title", "phone"]
    # Fields the contact doc already carries (no need to pull from lead)
    CONTACT_SHARED = {"lead_id", "company", "domain", "website", "country", "linkedin"}

    # Build a lookup: lead_id -> lead dict
    lead_by_id: dict[str, dict] = {}
    for r in lead_rows:
        lid = r.get("lead_id") or r.get("domain", "")
        if lid:
            lead_by_id[lid] = r

    flat_rows: list[dict] = []

    # 1. One row per contact-with-email
    for c in contact_rows:
        lid  = c.get("lead_id") or ""
        lead = lead_by_id.get(lid, {})
        row  = {}
        # Contact-specific columns first
        for col_name_c in CONTACT_COLS:
            row[col_name_c] = c.get(col_name_c, "")
        # Shared fields (prefer contact value, fall back to lead)
        for k in CONTACT_SHARED:
            row[k] = c.get(k) or lead.get(k, "")
        # All remaining lead fields that aren't already in the row
        for k, v in lead.items():
            if k not in row:
                row[k] = v
        # Resolve best phone: prefer contact-specific phone, fall back to
        # first number in the lead-level 'phones' field (comma-separated).
        row["phone"] = _best_phone(
            contact_phone=c.get("phone", ""),
            lead_phones=lead.get("phones", ""),
        )
        flat_rows.append(row)

    # 2. Leads with no email contact — one row each, contact cols empty
    lids_with_contact = {c.get("lead_id") or "" for c in contact_rows}
    for r in lead_rows:
        lid = r.get("lead_id") or r.get("domain", "")
        if lid not in lids_with_contact:
            row = {col_name_c: "" for col_name_c in CONTACT_COLS}
            row.update(r)
            row["phone"] = _best_phone(
                contact_phone="",
                lead_phones=r.get("phones", ""),
            )
            flat_rows.append(row)

    flat_df = pd.DataFrame(flat_rows) if flat_rows else pd.DataFrame()

    # Drop noisy phone columns — we keep only the single resolved 'phone'.
    # Also drop email_phones which is an internal scraping artefact.
    DROP_COLS = {"phones", "email_phones", "email_names"}
    if not flat_df.empty:
        flat_df.drop(columns=[c for c in DROP_COLS if c in flat_df.columns], inplace=True)

    # Put contact columns first, then sort remaining columns alphabetically
    if not flat_df.empty:
        front = [c for c in CONTACT_COLS if c in flat_df.columns]
        rest  = sorted(c for c in flat_df.columns if c not in front)
        flat_df = flat_df[front + rest]
        # Sort by score desc, then email asc
        sort_cols = []
        if "reseller_score" in flat_df.columns:
            flat_df["reseller_score"] = pd.to_numeric(
                flat_df["reseller_score"], errors="coerce"
            ).fillna(0).astype(int)
            sort_cols.append(("reseller_score", False))
        if "email" in flat_df.columns:
            sort_cols.append(("email", True))
        if sort_cols:
            flat_df = flat_df.sort_values(
                [s[0] for s in sort_cols],
                ascending=[s[1] for s in sort_cols],
            )

    # ------------------------------------------------------------------
    # Summary sheet
    # ------------------------------------------------------------------
    summary_rows = [
        {"metric": "Leads matched",     "value": len(lead_rows)},
        {"metric": "Leads with email",  "value": len(leads_with_email)},
        {"metric": "Contact rows",      "value": len(contact_rows)},
        {"metric": "Total rows (flat)", "value": len(flat_rows)},
        {"metric": "Score range",       "value": f"{min_score}–{max_score}"},
        {"metric": "Countries filter",  "value": ", ".join(countries) if countries else "all"},
        {"metric": "Source filter",     "value": source or "all"},
        {"metric": "Query filter",      "value": query or ""},
        {"metric": "Priority filter",   "value": ", ".join(priorities) if priorities else "all"},
        {"metric": "Keywords filter",   "value": ", ".join(keywords) if keywords else ""},
        {"metric": "Limit",             "value": limit if limit else ""},
        {"metric": "Extract name",      "value": save_extract or ""},
        {"metric": "Collection",        "value": col_name},
        {"metric": "Generated at",      "value": datetime.now().isoformat(timespec="seconds")},
    ]

    # ------------------------------------------------------------------
    # Write Excel
    # ------------------------------------------------------------------
    if not out_file:
        if _extract_file_stem:
            out_file = f"{_extract_file_stem}.xlsx"
        else:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_file = f"extract_leads_{ts}.xlsx"

    out_path = output_dir / out_file

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        _write_sheet(writer, "Extract",  flat_df)    # primary: one row per email
        _write_sheet(writer, "Leads",    leads_df)   # one row per lead (raw)
        pd.DataFrame(summary_rows).to_excel(writer, sheet_name="Summary", index=False)
        for sheet_name in ("Extract", "Leads", "Summary"):
            _autofit(writer.book[sheet_name])

    print(f"[extract_leads] {len(flat_rows)} rows ({len(contact_rows)} with email, "
          f"{len(flat_rows) - len(contact_rows)} without) → {out_path}")

    # ------------------------------------------------------------------
    # Save extract to Firestore  (only when --save-extract is given)
    # ------------------------------------------------------------------
    if save_extract:
        _save_extract_to_firestore(
            db=db,
            col=col,
            extract_name=save_extract,
            lead_rows=lead_rows,
            contact_rows=contact_rows,
            filters={
                "min_score":     min_score,
                "max_score":     max_score,
                "countries":     countries or [],
                "source":        source or "",
                "query":         query or "",
                "priorities":    priorities or [],
                "ai_potentials": ai_potentials or [],
                "keywords":      keywords or [],
            },
            dry_run=extract_dry_run,
        )

    return out_path


# ---------------------------------------------------------------------------
# Re-build Excel from an existing Firestore extract (already-exists path)
# ---------------------------------------------------------------------------

def _excel_from_existing_extract(
    db,
    extract_id: str,
    output_dir: Path,
    out_file: str | None,
) -> Path | None:
    """Read leads_extracted + contacts_extracted from an existing extract document
    and write (or overwrite) its Excel file without touching any Firestore data.
    """
    ex_doc = db.collection("leads_extract").document(extract_id)
    header = ex_doc.get().to_dict() or {}

    print(f"[extract] Re-reading '{extract_id}' from Firestore…")

    PROGRESS_EVERY = 100
    lead_rows: list[dict] = []
    contact_rows: list[dict] = []

    for ldoc in ex_doc.collection("leads_extracted").stream():
        lead_rows.append(ldoc.to_dict() or {})
        if len(lead_rows) % PROGRESS_EVERY == 0:
            print(f"[extract] {len(lead_rows)} leads read…")
        for cdoc in ldoc.reference.collection("contacts_extracted").stream():
            contact_rows.append(cdoc.to_dict() or {})

    print(f"[extract] {len(lead_rows)} leads, {len(contact_rows)} contacts read from existing extract.")

    # Reconstruct the same DataFrames the normal path produces
    leads_df = pd.DataFrame(lead_rows) if lead_rows else pd.DataFrame()
    _LEADS_DROP = {"email_phones", "email_names"}
    if not leads_df.empty:
        leads_df.drop(columns=[c for c in _LEADS_DROP if c in leads_df.columns], inplace=True)
    if not leads_df.empty and "reseller_score" in leads_df.columns:
        leads_df["reseller_score"] = pd.to_numeric(
            leads_df["reseller_score"], errors="coerce"
        ).fillna(0).astype(int)

    CONTACT_COLS  = ["email", "name", "title", "phone"]
    CONTACT_SHARED = {"lead_id", "company", "domain", "website", "country", "linkedin"}
    lead_by_id: dict[str, dict] = {}
    for r in lead_rows:
        lid = r.get("lead_id") or r.get("domain", "")
        if lid:
            lead_by_id[lid] = r

    flat_rows: list[dict] = []
    for c in contact_rows:
        lid  = c.get("lead_id") or ""
        lead = lead_by_id.get(lid, {})
        row  = {}
        for col_name_c in CONTACT_COLS:
            row[col_name_c] = c.get(col_name_c, "")
        for k in CONTACT_SHARED:
            row[k] = c.get(k) or lead.get(k, "")
        for k, v in lead.items():
            if k not in row:
                row[k] = v
        row["phone"] = _best_phone(
            contact_phone=c.get("phone", ""),
            lead_phones=lead.get("phones", ""),
        )
        flat_rows.append(row)

    lids_with_contact = {c.get("lead_id") or "" for c in contact_rows}
    for r in lead_rows:
        lid = r.get("lead_id") or r.get("domain", "")
        if lid not in lids_with_contact:
            row = {col_name_c: "" for col_name_c in CONTACT_COLS}
            row.update(r)
            row["phone"] = _best_phone(
                contact_phone="",
                lead_phones=r.get("phones", ""),
            )
            flat_rows.append(row)

    flat_df = pd.DataFrame(flat_rows) if flat_rows else pd.DataFrame()
    DROP_COLS = {"phones", "email_phones", "email_names"}
    if not flat_df.empty:
        flat_df.drop(columns=[c for c in DROP_COLS if c in flat_df.columns], inplace=True)
    if not flat_df.empty:
        front = [c for c in CONTACT_COLS if c in flat_df.columns]
        rest  = sorted(c for c in flat_df.columns if c not in front)
        flat_df = flat_df[front + rest]
        sort_cols = []
        if "reseller_score" in flat_df.columns:
            flat_df["reseller_score"] = pd.to_numeric(
                flat_df["reseller_score"], errors="coerce"
            ).fillna(0).astype(int)
            sort_cols.append(("reseller_score", False))
        if "email" in flat_df.columns:
            sort_cols.append(("email", True))
        if sort_cols:
            flat_df = flat_df.sort_values(
                [s[0] for s in sort_cols],
                ascending=[s[1] for s in sort_cols],
            )

    filters = header.get("filters") or {}
    summary_rows = [
        {"metric": "Leads matched",     "value": len(lead_rows)},
        {"metric": "Leads with email",  "value": len(lids_with_contact)},
        {"metric": "Contact rows",      "value": len(contact_rows)},
        {"metric": "Total rows (flat)", "value": len(flat_rows)},
        {"metric": "Extract name",      "value": extract_id},
        {"metric": "Original created",  "value": header.get("created_at", "")},
        {"metric": "Re-exported at",    "value": datetime.now().isoformat(timespec="seconds")},
        {"metric": "Score range",       "value": f"{filters.get('min_score', '')}–{filters.get('max_score', '')}"},
        {"metric": "Countries filter",  "value": ", ".join(filters.get("countries") or []) or "all"},
        {"metric": "Keywords filter",   "value": ", ".join(filters.get("keywords") or [])},
    ]

    if not out_file:
        out_file = f"{extract_id}.xlsx"
    out_path = output_dir / out_file

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        _write_sheet(writer, "Extract",  flat_df)
        _write_sheet(writer, "Leads",    leads_df)
        pd.DataFrame(summary_rows).to_excel(writer, sheet_name="Summary", index=False)
        for sheet_name in ("Extract", "Leads", "Summary"):
            _autofit(writer.book[sheet_name])

    print(f"[extract] Excel written → {out_path}  ({len(flat_rows)} rows)")
    return out_path


# ---------------------------------------------------------------------------
# Firestore extract persistence
# ---------------------------------------------------------------------------

def _save_extract_to_firestore(
    db,
    col,                   # main leads collection ref
    extract_name: str,
    lead_rows: list[dict],
    contact_rows: list[dict],
    filters: dict,
    dry_run: bool,
) -> None:
    """Write the extract to leads_extract/{extract_id}/leads_extracted/…
    and mark each lead in the main collection with extract_id.
    """
    from datetime import timezone

    # Build a safe document ID from the name
    extract_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", extract_name).strip("_") or "extract"

    EXTRACT_COLLECTION = "leads_extract"
    ex_col  = db.collection(EXTRACT_COLLECTION)
    ex_doc  = ex_col.document(extract_id)

    # Build a contact lookup: lead_id → [contact dicts]
    contacts_by_lead: dict[str, list[dict]] = {}
    for c in contact_rows:
        lid = c.get("lead_id") or ""
        if lid:
            contacts_by_lead.setdefault(lid, []).append(c)

    total_leads    = len(lead_rows)
    total_contacts = sum(len(v) for v in contacts_by_lead.values())

    tag = " [DRY RUN]" if dry_run else ""
    print(f"\n[extract] {'Saving' if not dry_run else 'Would save'} extract "
          f"'{extract_id}' → {EXTRACT_COLLECTION}/{extract_id}{tag}")
    print(f"[extract] {total_leads} leads, {total_contacts} contacts")

    if dry_run:
        print(f"[extract] DRY RUN — nothing written to Firestore.")
        # Print a short preview
        for r in lead_rows[:10]:
            lid = r.get("lead_id") or r.get("domain", "")
            nc  = len(contacts_by_lead.get(lid, []))
            print(f"  {r.get('domain',''):<50}  {r.get('country',''):<4}  "
                  f"score={r.get('reseller_score','')}  contacts={nc}")
        if total_leads > 10:
            print(f"  … and {total_leads - 10} more leads")
        return

    # Write the extract header document (contact_count updated after writing)
    ex_doc.set({
        "extract_id":    extract_id,
        "name":          extract_name,
        "created_at":    datetime.now(tz=timezone.utc).isoformat(),
        "lead_count":    total_leads,
        "contact_count": 0,          # updated with real count once writing is done
        "filters":       filters,
    })

    PROGRESS_EVERY = 100
    leads_written    = 0
    contacts_written = 0             # real count of contacts stored

    for lead in lead_rows:
        lid      = lead.get("lead_id") or lead.get("domain", "")
        if not lid:
            continue

        # Write lead to leads_extracted sub-collection
        lead_doc = dict(lead)
        lead_doc["extract_id"] = extract_id
        ex_doc.collection("leads_extracted").document(lid).set(lead_doc)

        # Write contacts to contacts_extracted sub-sub-collection
        for contact in contacts_by_lead.get(lid, []):
            import hashlib as _hl
            cid = _hl.sha1((contact.get("email") or lid).lower().encode()).hexdigest()[:10]
            (ex_doc.collection("leads_extracted").document(lid)
                   .collection("contacts_extracted").document(cid)
                   .set(contact))
            contacts_written += 1

        leads_written += 1
        if leads_written % PROGRESS_EVERY == 0:
            print(f"[extract] {leads_written}/{total_leads} leads saved…")

    # Update header with the real contact count
    ex_doc.update({"contact_count": contacts_written})

    print(f"[extract] Saved {leads_written} leads + {contacts_written} contacts "
          f"→ {EXTRACT_COLLECTION}/{extract_id} ✓")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _best_phone(contact_phone: str, lead_phones: str) -> str:
    """Return the single most promising phone number.

    Priority:
    1. The contact-specific phone (was paired to this email during scraping).
    2. First mobile-looking number from the lead's page-level phones list
       (starts with +47 9/4, +46 7, +45 4/9, +49 15/16/17, +44 7, etc.).
    3. First number from the lead's phones list as a fallback.
    """
    import re

    contact_phone = (contact_phone or "").strip()
    if contact_phone:
        return contact_phone

    lead_phones = (lead_phones or "").strip()
    if not lead_phones:
        return ""

    candidates = [p.strip() for p in lead_phones.split(",") if p.strip()]
    if not candidates:
        return ""

    # Patterns that look like mobile numbers for the main target countries
    MOBILE_RE = re.compile(
        r"(?:\+47\s*[49]"       # Norway mobile
        r"|\+46\s*7"            # Sweden mobile
        r"|\+45\s*[49]"         # Denmark mobile
        r"|\+49\s*1[567]"       # Germany mobile
        r"|\+44\s*7"            # UK mobile
        r"|^(?:4|9)\d{7}$"      # bare 8-digit NO/DK mobile
        r"|^07\d{9}$"           # bare UK mobile
        r")"
    )
    for p in candidates:
        digits_only = re.sub(r"\D", "", p)
        if MOBILE_RE.search(p) or MOBILE_RE.search(digits_only):
            return p

    # No mobile found — return first candidate
    return candidates[0]


def _write_sheet(writer, name, df):
    import pandas as pd
    if df.empty:
        pd.DataFrame().to_excel(writer, sheet_name=name, index=False)
    else:
        df.to_excel(writer, sheet_name=name, index=False)


def _autofit(ws):
    ws.freeze_panes = "A2"
    for col in ws.columns:
        max_len = min(max(len(str(c.value or "")) for c in col) + 2, 60)
        ws.column_dimensions[col[0].column_letter].width = max_len


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def _parse_args(argv=None):
    import argparse
    p = argparse.ArgumentParser(
        description="Extract and filter leads from Firestore into a new Excel file."
    )
    p.add_argument("--collection", metavar="NAME",
                   help="Firestore collection name (default: leads / FIRESTORE_COLLECTION env var)")
    p.add_argument("--output",    default=None,
                   help="Directory to write the Excel file (default: <project_root>/output)")
    p.add_argument("--min-score", type=int, default=0,
                   help="Minimum reseller_score (default: 0)")
    p.add_argument("--max-score", type=int, default=100,
                   help="Maximum reseller_score (default: 100)")
    p.add_argument("--country",   action="append", dest="countries", metavar="CODE",
                   help="Country code to include (repeatable, e.g. --country NO --country SE)")
    p.add_argument("--source",    choices=["search", "catalog", "both"],
                   help="Filter by discovery source: search | catalog | both")
    p.add_argument("--query",     metavar="TEXT",
                   help="Substring match on source_query (case-insensitive)")
    p.add_argument("--priority",  action="append", dest="priorities", metavar="P",
                   help="Priority label (repeatable, e.g. --priority A --priority B)")
    p.add_argument("--ai-potential", action="append", dest="ai_potentials", metavar="LEVEL",
                   help="Filter by ai_reseller_potential: high, medium, low (repeatable)")
    p.add_argument("--with-email", action="store_true",
                   help="Only include leads with at least one contact email")
    p.add_argument("--keywords",  metavar="KW",
                   help="Comma-separated keywords (OR logic); lead matches if any keyword "
                        "appears in source_query, title, description, company, domain, "
                        "website, keywords, or reasons. E.g. --keywords wordpress,woocommerce")
    p.add_argument("--out",       metavar="FILE",
                   help="Output filename (placed in --output dir; default: auto-generated)")
    p.add_argument("--limit",        type=int, default=None,
                   help="Maximum number of leads to include (applied after all filters)")
    p.add_argument("--save-extract", metavar="NAME", nargs="?", const="__dry_run__",
                   help="Save this extract to Firestore as leads_extract/<NAME>. "
                        "If NAME is omitted the flag acts as a dry-run preview only — "
                        "nothing is written to Firestore. "
                        "Leads already claimed by another extract are skipped.")
    p.add_argument("--allow-reextract", action="store_true",
                   help="Skip the already-extracted check — allow leads to appear in multiple extracts. "
                        "Use when re-running QQ or force-recrawled leads.")
    p.add_argument("--extract-dry-run", action="store_true",
                   help="Preview what --save-extract would write without touching Firestore.")
    p.add_argument("--auto-name", action="store_true",
                   help="Auto-generate --save-extract name from filters: "
                        "e.g. UK_score70_high_jun01. Overrides --save-extract name.")
    return p.parse_args(argv)


def main(argv=None):
    import sys
    args = _parse_args(argv)

    # Expand comma-separated country codes so both styles work:
    #   --country NO,SE      →  ["NO", "SE"]
    #   --country NO --country SE  →  ["NO", "SE"]
    countries = None
    if args.countries:
        expanded = []
        for c in args.countries:
            expanded.extend(x.strip().upper() for x in c.split(",") if x.strip())
        countries = expanded or None

    # Same for priorities
    priorities = None
    if args.priorities:
        expanded = []
        for p in args.priorities:
            expanded.extend(x.strip().upper() for x in p.split(",") if x.strip())
        priorities = expanded or None

    # Parse --ai-potential
    ai_potentials = None
    if getattr(args, "ai_potentials", None):
        ai_potentials = [p.strip().lower() for p in args.ai_potentials if p.strip()]
        ai_potentials = ai_potentials or None

    # Parse --keywords: accept both comma-separated and repeated --keywords flags
    keywords = None
    if args.keywords:
        keywords = [k.strip().lower() for k in args.keywords.split(",") if k.strip()]
        keywords = keywords or None

    if countries:
        print(f"[extract_leads] Country filter: {countries}")
    if priorities:
        print(f"[extract_leads] Priority filter: {priorities}")
    if keywords:
        print(f"[extract_leads] Keyword filter (OR): {keywords}")

    # Build auto-name from active filters when --auto-name is set
    save_extract = None if args.save_extract == "__dry_run__" else args.save_extract
    if getattr(args, "auto_name", False):
        from datetime import datetime as _dt
        parts = []
        if countries:
            parts.append("_".join(countries))
        if args.min_score and args.min_score > 0:
            parts.append(f"score{args.min_score}")
        if args.max_score and args.max_score < 100:
            parts.append(f"max{args.max_score}")
        if priorities:
            parts.append("_".join(p.lower() for p in priorities))
        if ai_potentials:
            parts.append("_".join(p.lower() for p in ai_potentials))
        if args.source:
            parts.append(args.source)
        parts.append(_dt.now().strftime("%b%d").lower())  # e.g. jun02
        save_extract = "_".join(parts) if parts else f"extract_{_dt.now().strftime('%b%d').lower()}"
        print(f"[extract_leads] Auto-name: {save_extract!r}")

    try:
        path = extract_leads(
            output_dir=args.output,
            min_score=args.min_score,
            max_score=args.max_score,
            countries=countries,
            source=args.source,
            query=args.query,
            priorities=priorities,
            with_email=args.with_email,
            out_file=args.out,
            collection=args.collection,
            keywords=keywords,
            ai_potentials=ai_potentials,
            allow_reextract=getattr(args, "allow_reextract", False),
            save_extract=save_extract,
            extract_dry_run=args.extract_dry_run or (save_extract is None and args.save_extract == "__dry_run__"),
            limit=args.limit,
        )
        if path:
            print(f"Saved: {path}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
