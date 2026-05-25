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

import argparse
import re
import importlib.util
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

# Import name-validation helper from utils (same package)
import sys as _sys
import os as _os
_sys.path.insert(0, _os.path.dirname(__file__))
from utils import clean_contact_name as _clean_contact_name


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
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)

    db  = firestore.client()
    col = db.collection(col_name)
    return db, col


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def extract_leads(
    output_dir: str | Path | None = None,
    min_score: int = 0,
    max_score: int = 100,
    countries: list[str] | None = None,
    source: str | None = None,
    query: str | None = None,
    priorities: list[str] | None = None,
    with_email: bool = False,
    out_file: str | None = None,
    collection: str | None = None,
) -> Path:
    """Filter leads from Firestore and write a focused Excel extract.

    Parameters
    ----------
    output_dir  : directory to write the output Excel file
    min_score   : minimum reseller_score (inclusive)
    max_score   : maximum reseller_score (inclusive)
    countries   : list of country codes, e.g. ["NO", "SE"] — None = all
    source      : "search"  → found_by_search == "yes"
                  "catalog" → found_by_catalog == "yes"
                  "both"    → found by BOTH modes
                  None      → no source filter
    query       : substring match on source_query (case-insensitive)
    priorities  : list of priority labels, e.g. ["A", "B"] — None = all
    with_email  : if True, only include leads with ≥1 contact email
    out_file    : filename for the output Excel file (placed in output_dir);
                  defaults to extract_leads_YYYYMMDD_HHMMSS.xlsx
    collection  : Firestore collection name (default: "leads" /
                  FIRESTORE_COLLECTION env var)

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
    # Load all lead documents
    # ------------------------------------------------------------------
    country_upper = [c.upper() for c in countries] if countries else None
    priority_upper = [p.upper() for p in priorities] if priorities else None

    lead_rows: list[dict] = []
    skipped = 0
    _country_sample: list[str] = []   # for diagnostics

    for doc in col.stream():
        d = doc.to_dict()
        if not d:
            continue

        # --- score ---
        score = 0
        try:
            score = int(float(d.get("reseller_score", 0) or 0))
        except (ValueError, TypeError):
            pass
        if score < min_score or score > max_score:
            skipped += 1
            continue

        # --- country ---
        raw_country = (d.get("country") or "").upper()
        if len(_country_sample) < 10 and raw_country:
            _country_sample.append(raw_country)
        if country_upper:
            if raw_country not in country_upper:
                skipped += 1
                continue

        # --- source ---
        if source == "search":
            if (d.get("found_by_search") or "").lower() != "yes":
                skipped += 1
                continue
        elif source == "catalog":
            if (d.get("found_by_catalog") or "").lower() != "yes":
                skipped += 1
                continue
        elif source == "both":
            if not ((d.get("found_by_search") or "").lower() == "yes" and
                    (d.get("found_by_catalog") or "").lower() == "yes"):
                skipped += 1
                continue

        # --- query substring ---
        if query:
            sq = (d.get("source_query") or "").lower()
            if query.lower() not in sq:
                skipped += 1
                continue

        # --- priority ---
        if priority_upper:
            if (d.get("priority") or "").upper() not in priority_upper:
                skipped += 1
                continue

        lead_rows.append(d)

    if country_upper and _country_sample:
        print(f"[extract_leads] Sample 'country' values in Firestore: {sorted(set(_country_sample))}")
    print(f"[extract_leads] {len(lead_rows)} leads matched, {skipped} filtered out")

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
        flat_rows.append(row)

    # 2. Leads with no email contact — one row each, contact cols empty
    lids_with_contact = {c.get("lead_id") or "" for c in contact_rows}
    for r in lead_rows:
        lid = r.get("lead_id") or r.get("domain", "")
        if lid not in lids_with_contact:
            row = {col_name_c: "" for col_name_c in CONTACT_COLS}
            row.update(r)
            flat_rows.append(row)

    flat_df = pd.DataFrame(flat_rows) if flat_rows else pd.DataFrame()

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
        {"metric": "Collection",        "value": col_name},
        {"metric": "Generated at",      "value": datetime.now().isoformat(timespec="seconds")},
    ]

    # ------------------------------------------------------------------
    # Write Excel
    # ------------------------------------------------------------------
    if not out_file:
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
    return out_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
    p.add_argument("--with-email", action="store_true",
                   help="Only include leads with at least one contact email")
    p.add_argument("--out",       metavar="FILE",
                   help="Output filename (placed in --output dir; default: auto-generated)")
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

    if countries:
        print(f"[extract_leads] Country filter: {countries}")
    if priorities:
        print(f"[extract_leads] Priority filter: {priorities}")

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
        )
        print(f"Saved: {path}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
