"""Statistics aggregation -- reads Firestore leads and writes summary docs + Excel.

Usage:
    python app/statistics.py              # Firestore write + Excel export
    python app/statistics.py --no-excel   # Firestore write only
    python app/statistics.py --excel-only # Excel from existing Firestore data

Firestore structure written:

    statistics/priority-pr-country                    <- head document (grand totals + priority summary)
    statistics/priority-pr-country/countries/NO       <- one sub-doc per country
    statistics/priority-pr-country/countries/SE
    ...

Head document includes a by_priority summary across ALL countries:
    {
        "generated_at":   "...",
        "total_leads":    999,
        "total_contacts": 2345,
        "country_codes":  ["DE", "NO", "SE"],
        "by_priority": {
            "A":     {"leads": 50,  "contacts": 120},
            "B":     {"leads": 200, "contacts": 500},
            "unset": {"leads": 749, "contacts": 1725}
        }
    }
"""
from __future__ import annotations

import _pathsetup  # noqa: F401 — adds project root, app/, app/functions/, app/collect-functions/ to sys.path
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Credential loader
# ---------------------------------------------------------------------------

def _get_credentials():
    try:
        import firebase_admin.credentials as fb_creds
    except ImportError:
        raise SystemExit("firebase-admin not installed. Run: pip install firebase-admin")

    secrets_path = Path(__file__).parent.parent / "blueboot_secrets.py"
    if secrets_path.exists():
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("blueboot_secrets", secrets_path)
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            key_dict = getattr(mod, "fireBaseAdminKey", None)
            if key_dict:
                return fb_creds.Certificate(key_dict)
        except Exception as exc:
            print(f"  [firebase] could not load blueboot_secrets: {exc}")

    creds_path = os.getenv("FIREBASE_CREDENTIALS", "config/serviceAccountKey.json")
    if Path(creds_path).exists():
        return fb_creds.Certificate(creds_path)

    raise SystemExit(
        "No Firebase credentials found.\n"
        "Set FIREBASE_CREDENTIALS or place blueboot_secrets.py in the project root."
    )


def _init_firebase():
    import firebase_admin
    from firebase_admin import firestore
    if not firebase_admin._apps:
        firebase_admin.initialize_app(_get_credentials())
    return firestore.client()


# ---------------------------------------------------------------------------
# Aggregation + Firestore write
# ---------------------------------------------------------------------------

def summarise_country_pr_priority(
    leads_collection=None,
    stats_collection="statistics",
    head_doc_id="priority-pr-country",
):
    """Aggregate leads + contacts per (country, priority) and write to Firestore.

    Head doc  : statistics/priority-pr-country
                Includes a grand by_priority summary across all countries.
    Sub-docs  : statistics/priority-pr-country/countries/{ISO_code}

    Returns dict with keys:
        "head"      -> head document dict
        "countries" -> {ISO_code: country_doc_dict}
    """
    db = _init_firebase()
    col_name  = leads_collection or os.getenv("FIRESTORE_COLLECTION", "leads")
    leads_col = db.collection(col_name)

    # ------------------------------------------------------------------
    # Pass 1: stream all lead docs
    # ------------------------------------------------------------------
    print(f"  [stats] streaming leads from '{col_name}' ...")
    lead_meta = {}

    for doc in leads_col.select(["country", "country_name", "priority", "lead_id"]).stream():
        data         = doc.to_dict()
        lid          = data.get("lead_id") or doc.id
        country      = (data.get("country")      or "").strip().upper() or "XX"
        country_name = (data.get("country_name") or "").strip() or country
        priority     = (data.get("priority")     or "").strip()
        lead_meta[lid] = {
            "country":      country,
            "country_name": country_name,
            "priority":     priority,
        }

    print(f"  [stats] {len(lead_meta)} lead docs loaded.")

    # ------------------------------------------------------------------
    # Pass 2: count contacts per lead via collection_group
    # ------------------------------------------------------------------
    print("  [stats] counting contacts via collection_group('contacts') ...")
    contacts_per_lead = defaultdict(int)

    for doc in db.collection_group("contacts").select(["lead_id"]).stream():
        data = doc.to_dict()
        lid  = data.get("lead_id") or doc.reference.parent.parent.id
        if lid:
            contacts_per_lead[lid] += 1

    print(f"  [stats] {sum(contacts_per_lead.values())} contact docs counted.")

    # ------------------------------------------------------------------
    # Aggregate per (country, priority)
    # NOTE: Firestore rejects empty-string map keys -> use "unset"
    # ------------------------------------------------------------------
    agg           = defaultdict(lambda: defaultdict(lambda: {"leads": 0, "contacts": 0}))
    country_names = {}

    for lid, meta in lead_meta.items():
        code     = meta["country"]
        priority = meta["priority"] or "unset"
        country_names[code] = meta["country_name"]
        agg[code][priority]["leads"]    += 1
        agg[code][priority]["contacts"] += contacts_per_lead.get(lid, 0)

    # ------------------------------------------------------------------
    # Roll up a grand by_priority summary across all countries
    # ------------------------------------------------------------------
    grand_by_priority = defaultdict(lambda: {"leads": 0, "contacts": 0})
    for by_prio in agg.values():
        for prio, counts in by_prio.items():
            grand_by_priority[prio]["leads"]    += counts["leads"]
            grand_by_priority[prio]["contacts"] += counts["contacts"]

    # ------------------------------------------------------------------
    # Write to Firestore
    # ------------------------------------------------------------------
    generated_at  = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    head_ref      = db.collection(stats_collection).document(head_doc_id)
    countries_col = head_ref.collection("countries")

    country_results = {}
    grand_leads     = 0
    grand_contacts  = 0

    for code, by_prio in sorted(agg.items()):
        total_leads    = sum(v["leads"]    for v in by_prio.values())
        total_contacts = sum(v["contacts"] for v in by_prio.values())
        grand_leads    += total_leads
        grand_contacts += total_contacts

        country_doc = {
            "country":          code,
            "country_name":     country_names.get(code, code),
            "generated_at":     generated_at,
            "leads_collection": col_name,
            "total_leads":      total_leads,
            "total_contacts":   total_contacts,
            "by_priority":      dict(sorted(by_prio.items())),
        }
        countries_col.document(code).set(country_doc)
        country_results[code] = country_doc
        print(
            f"  [stats] written -> {stats_collection}/{head_doc_id}/countries/{code}"
            f"  ({total_leads} leads, {total_contacts} contacts)"
        )

    head_doc = {
        "generated_at":     generated_at,
        "leads_collection": col_name,
        "total_leads":      grand_leads,
        "total_contacts":   grand_contacts,
        "country_codes":    sorted(agg.keys()),
        "by_priority":      dict(sorted(grand_by_priority.items())),
    }
    head_ref.set(head_doc)
    print(
        f"  [stats] written -> {stats_collection}/{head_doc_id}"
        f"  (grand total: {grand_leads} leads, {grand_contacts} contacts)"
    )

    return {"head": head_doc, "countries": country_results}


# ---------------------------------------------------------------------------
# Excel export
# ---------------------------------------------------------------------------

def export_to_excel(results, outdir=None):
    """Write statistics to an Excel file with three sheets.

    Sheets:
        Summary         -- grand totals + by_priority across all countries
        By Country      -- one row per country (totals)
        Country x Prio  -- one row per (country, priority) combination

    Parameters
    ----------
    results : dict
        Return value of summarise_country_pr_priority().
    outdir : str or Path or None
        Output directory.  Defaults to <project_root>/output.

    Returns
    -------
    Path to the written Excel file.
    """
    import pandas as pd

    head      = results["head"]
    countries = results["countries"]

    outdir = Path(outdir) if outdir else Path(__file__).parent.parent / "output"
    outdir.mkdir(parents=True, exist_ok=True)
    out_path = outdir / "statistics.xlsx"

    # --- Sheet 1: Summary ---
    summary_rows = [
        {"metric": "Generated at",   "value": head["generated_at"]},
        {"metric": "Leads collection","value": head["leads_collection"]},
        {"metric": "Total leads",    "value": head["total_leads"]},
        {"metric": "Total contacts", "value": head["total_contacts"]},
        {"metric": "Countries",      "value": ", ".join(head["country_codes"])},
    ]
    summary_df = pd.DataFrame(summary_rows)

    prio_rows = [
        {"priority": prio, "leads": v["leads"], "contacts": v["contacts"]}
        for prio, v in sorted(head["by_priority"].items())
    ]
    prio_df = pd.DataFrame(prio_rows)

    # --- Sheet 2: By Country ---
    country_rows = [
        {
            "country":      data["country"],
            "country_name": data["country_name"],
            "total_leads":  data["total_leads"],
            "total_contacts": data["total_contacts"],
        }
        for data in countries.values()
    ]
    country_df = pd.DataFrame(country_rows)

    # --- Sheet 3: Country x Priority ---
    detail_rows = []
    for data in countries.values():
        for prio, counts in data["by_priority"].items():
            detail_rows.append({
                "country":      data["country"],
                "country_name": data["country_name"],
                "priority":     prio,
                "leads":        counts["leads"],
                "contacts":     counts["contacts"],
            })
    detail_df = pd.DataFrame(detail_rows)

    # --- Write ---
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="Summary",        index=False)
        prio_df.to_excel(   writer, sheet_name="By Priority",    index=False, startrow=len(summary_df) + 2)
        country_df.to_excel(writer, sheet_name="By Country",     index=False)
        detail_df.to_excel( writer, sheet_name="Country x Prio", index=False)

        # Auto-fit columns on all sheets
        for sheet in writer.book.worksheets:
            for col in sheet.columns:
                max_len = max((len(str(cell.value or "")) for cell in col), default=0)
                sheet.column_dimensions[col[0].column_letter].width = min(max_len + 2, 50)

    print(f"  [stats] Excel written -> {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# Pretty-print helper
# ---------------------------------------------------------------------------

def _print_summary(results):
    head      = results["head"]
    countries = results["countries"]
    print()
    print("=" * 60)
    print("  Statistics: priority x country")
    print(f"  Generated   : {head['generated_at']}")
    print(f"  Grand total : {head['total_leads']} leads, {head['total_contacts']} contacts")
    print()
    print("  By priority (all countries):")
    for prio, v in head["by_priority"].items():
        print(f"    {prio:20s}  leads: {v['leads']:4d}   contacts: {v['contacts']:4d}")
    print("=" * 60)
    for code, data in countries.items():
        print(
            f"\n  [{code}] {data['country_name']}"
            f"  --  {data['total_leads']} leads, {data['total_contacts']} contacts"
        )
        for prio, counts in data["by_priority"].items():
            print(f"    {prio:20s}  leads: {counts['leads']:4d}   contacts: {counts['contacts']:4d}")
    print()


# ---------------------------------------------------------------------------
# Reasons count aggregation
# ---------------------------------------------------------------------------

def summarise_reasons_count(
    leads_collection=None,
    stats_collection="statistics",
    head_doc_id="reasons-count",
    writeback=True,
):
    """Aggregate reason strings per country and write to Firestore.

    Each lead's 'reasons' field is a semicolon-separated string of scored
    signals, e.g. "wordpress: site, plugins; agency language: web, design".
    This function splits them, counts occurrences per country, and writes:

        statistics/reasons-count                    <- head doc (global totals)
        statistics/reasons-count/countries/NO       <- one sub-doc per country
        statistics/reasons-count/countries/SE
        ...

    Reason counts are stored as a list of {reason, count} objects (sorted
    by count desc) because reason strings can contain characters that
    Firestore rejects as map keys (colons, slashes, etc.).

    Head doc shape:
    {
        "generated_at":     "...",
        "leads_collection": "leads",
        "total_leads":      999,
        "country_codes":    ["DE", "NO", "SE"],
        "reasons": [
            {"reason": "has services/customers/cases language", "count": 450},
            {"reason": "agency language: web, design, digital", "count": 320},
            ...
        ]
    }

    Country sub-doc shape:
    {
        "country":          "NO",
        "country_name":     "Norway",
        "generated_at":     "...",
        "leads_collection": "leads",
        "total_leads":      42,
        "reasons": [
            {"reason": "has services/customers/cases language", "count": 20},
            ...
        ]
    }

    Returns
    -------
    dict with keys "head" and "countries".
    """
    db = _init_firebase()
    col_name  = leads_collection or os.getenv("FIRESTORE_COLLECTION", "leads")
    leads_col = db.collection(col_name)

    print(f"  [reasons] streaming leads from '{col_name}' ...")

    # country_reasons: country_code -> reason_str -> count
    country_reasons  = defaultdict(lambda: defaultdict(int))
    country_names    = {}
    country_leads    = defaultdict(int)
    global_reasons   = defaultdict(int)
    total_leads      = 0

    # lead_reasons_list: doc_id -> [reason, ...] (parsed labels, for writeback)
    lead_reasons_list = {}

    fields = ["country", "country_name", "lead_id", "reasons"]
    for doc in leads_col.select(fields).stream():
        data         = doc.to_dict()
        country      = (data.get("country")      or "").strip().upper() or "XX"
        country_name = (data.get("country_name") or "").strip() or country
        reasons_raw  = (data.get("reasons")      or "").strip()

        country_names[country] = country_name
        country_leads[country] += 1
        total_leads += 1

        parsed_reasons = []
        if reasons_raw:
            for part in reasons_raw.split(";"):
                segment = part.strip()
                if not segment:
                    continue
                # Take the label part (left of ":"), e.g.:
                #   "wordpress: site, plugins"  -> "wordpress"
                #   "NON-AGENCY penalty: -20"   -> "NON-AGENCY penalty"
                #   "has services/customers/cases language" -> (no colon, kept whole)
                label = segment.split(":")[0].strip()
                if not label:
                    continue
                # Further split on "/" to expand compound labels, e.g.:
                #   "has services/customers/cases language"
                #   -> ["has services", "customers", "cases language"]
                sub_parts = [s.strip() for s in label.split("/") if s.strip()]
                for reason in sub_parts:
                    parsed_reasons.append(reason)
                    country_reasons[country][reason] += 1
                    global_reasons[reason]           += 1

        lead_reasons_list[doc.id] = parsed_reasons

    print(f"  [reasons] {total_leads} leads read, "
          f"{len(global_reasons)} distinct reason strings found.")

    # ------------------------------------------------------------------
    # Write reasons-list back to each lead document (batch, optional)
    # ------------------------------------------------------------------
    if writeback:
        total_wb = len(lead_reasons_list)
        print(f"  [reasons] writing reasons-list back to {total_wb} lead docs ...")
        MAX_BATCH      = 400
        PROGRESS_EVERY = 100
        batch = db.batch()
        ops   = 0
        done  = 0

        for doc_id, r_list in lead_reasons_list.items():
            batch.update(leads_col.document(doc_id), {"reasons-list": r_list})
            ops  += 1
            done += 1
            if ops >= MAX_BATCH:
                batch.commit()
                batch = db.batch()
                ops   = 0
            if done % PROGRESS_EVERY == 0:
                print(f"  [reasons] {done}/{total_wb} docs updated…")

        if ops:
            batch.commit()
        print(f"  [reasons] reasons-list written to all {total_wb} lead docs.")
    else:
        print(f"  [reasons] writeback skipped (writeback=False).")

    # ------------------------------------------------------------------
    # Write to Firestore
    # ------------------------------------------------------------------
    generated_at  = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    head_ref      = db.collection(stats_collection).document(head_doc_id)
    countries_col = head_ref.collection("countries")

    country_results = {}

    for code in sorted(country_reasons):
        reason_list = [
            {"reason": r, "count": c}
            for r, c in sorted(country_reasons[code].items(), key=lambda x: -x[1])
        ]
        country_doc = {
            "country":          code,
            "country_name":     country_names.get(code, code),
            "generated_at":     generated_at,
            "leads_collection": col_name,
            "total_leads":      country_leads[code],
            "reasons":          reason_list,
        }
        countries_col.document(code).set(country_doc)
        country_results[code] = country_doc
        print(f"  [reasons] written -> {stats_collection}/{head_doc_id}/countries/{code}"
              f"  ({len(reason_list)} distinct reasons)")

    global_reason_list = [
        {"reason": r, "count": c}
        for r, c in sorted(global_reasons.items(), key=lambda x: -x[1])
    ]
    head_doc = {
        "generated_at":     generated_at,
        "leads_collection": col_name,
        "total_leads":      total_leads,
        "country_codes":    sorted(country_reasons.keys()),
        "reasons":          global_reason_list,
    }
    head_ref.set(head_doc)
    print(f"  [reasons] written -> {stats_collection}/{head_doc_id}"
          f"  (grand total: {total_leads} leads, {len(global_reason_list)} reasons)")

    return {"head": head_doc, "countries": country_results}


def export_reasons_to_excel(results, outdir=None):
    """Write reasons-count statistics to output/statistics_reasons.xlsx.

    Sheets:
        Global Reasons  -- all reasons sorted by count desc (across all countries)
        By Country      -- one row per (country, reason) combination

    Parameters
    ----------
    results : dict
        Return value of summarise_reasons_count().
    outdir : str or Path or None
        Output directory. Defaults to <project_root>/output.

    Returns
    -------
    Path to the written Excel file.
    """
    import pandas as pd

    head      = results["head"]
    countries = results["countries"]

    outdir = Path(outdir) if outdir else Path(__file__).parent.parent / "output"
    outdir.mkdir(parents=True, exist_ok=True)
    out_path = outdir / "statistics_reasons.xlsx"

    # Sheet 1: global reasons
    global_df = pd.DataFrame(head["reasons"])
    if global_df.empty:
        global_df = pd.DataFrame(columns=["reason", "count"])

    # Sheet 2: per country x reason
    detail_rows = []
    for data in countries.values():
        for entry in data["reasons"]:
            detail_rows.append({
                "country":      data["country"],
                "country_name": data["country_name"],
                "reason":       entry["reason"],
                "count":        entry["count"],
            })
    detail_df = pd.DataFrame(detail_rows) if detail_rows else pd.DataFrame(
        columns=["country", "country_name", "reason", "count"]
    )
    # Sort by country then count desc
    if not detail_df.empty:
        detail_df = detail_df.sort_values(["country", "count"], ascending=[True, False])

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        global_df.to_excel(writer, sheet_name="Global Reasons", index=False)
        detail_df.to_excel(writer, sheet_name="By Country",     index=False)

        for sheet in writer.book.worksheets:
            for col in sheet.columns:
                max_len = max((len(str(cell.value or "")) for cell in col), default=0)
                sheet.column_dimensions[col[0].column_letter].width = min(max_len + 2, 80)

    print(f"  [reasons] Excel written -> {out_path}")
    return out_path

# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    from dotenv import load_dotenv
    load_dotenv()

    import argparse
    parser = argparse.ArgumentParser(
        description="Aggregate lead statistics (priority + reasons), write to Firestore and Excel.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--leads-collection", default=None,
        help="Firestore leads collection (default: FIRESTORE_COLLECTION env var or 'leads').",
    )
    parser.add_argument(
        "--stats-collection", default="statistics",
        help="Firestore collection for output documents.",
    )
    parser.add_argument(
        "--output", default=None,
        help="Directory for Excel files (default: <project_root>/output).",
    )
    parser.add_argument(
        "--no-excel", action="store_true",
        help="Skip writing Excel files.",
    )
    parser.add_argument(
        "--no-writeback", action="store_true",
        help="Skip writing reasons-list back to each lead document.",
    )
    parser.add_argument(
        "--only", choices=["priority", "reasons"], default=None,
        help="Run only one aggregation (default: run both).",
    )
    args = parser.parse_args()

    run_priority = args.only in (None, "priority")
    run_reasons  = args.only in (None, "reasons")

    if run_priority:
        print("\n--- Priority x Country ---")
        prio_results = summarise_country_pr_priority(
            leads_collection=args.leads_collection,
            stats_collection=args.stats_collection,
        )
        _print_summary(prio_results)
        if not args.no_excel:
            export_to_excel(prio_results, outdir=args.output)

    if run_reasons:
        print("\n--- Reasons Count ---")
        reason_results = summarise_reasons_count(
            leads_collection=args.leads_collection,
            stats_collection=args.stats_collection,
            writeback=not args.no_writeback,
        )
        if not args.no_excel:
            export_reasons_to_excel(reason_results, outdir=args.output)


if __name__ == "__main__":
    main()
