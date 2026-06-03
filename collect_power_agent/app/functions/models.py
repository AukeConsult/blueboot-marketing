"""Lead dataclass + export helpers."""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

from utils import load_lines


def lead_id_from_url(url: str) -> str:
    """Human-readable Firestore/Excel ID from a site URL.

    https://www.sol.no/  ->  www_sol_no
    vg.no                ->  vg_no
    """
    host = urlparse(url).hostname or url
    # strip trailing dot, replace dots and hyphens with underscores
    slug = re.sub(r"[.\-]+", "_", host.rstrip(".").lower())
    # collapse repeated underscores
    return re.sub(r"_+", "_", slug).strip("_")

# openpyxl rejects control characters (U+0000–U+001F) except tab (09), LF (0A), CR (0D)
_ILLEGAL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sanitize_df(df: pd.DataFrame) -> pd.DataFrame:
    """Strip illegal Excel characters from every string column in-place."""
    for col in df.select_dtypes(include="object").columns:
        df[col] = df[col].apply(
            lambda v: _ILLEGAL_CHARS.sub("", v) if isinstance(v, str) else v
        )
    return df


@dataclass
class Lead:
    company: str
    domain: str
    website: str
    source_query: str
    title: str = ""
    description: str = ""
    emails: str = ""
    email_titles: str = ""
    email_phones: str = ""
    email_names: str = ""
    phones: str = ""
    contact_page: str = ""
    linkedin: str = ""
    detected_tech: str = ""
    categories: str = ""
    reseller_score: int = 0
    priority: str = ""
    reasons: str = ""
    suggested_angle: str = ""
    status: str = "New"
    country: str = ""
    country_name: str = ""
    notes: str = ""
    crawled_at: str = ""
    found_by_search: str = ""   # "yes" when discovered via keyword search / GitHub pre-pass
    found_by_catalog: str = ""  # "yes" when discovered via directory catalog scrape


def dedupe_leads(leads: list[Lead]) -> list[Lead]:
    """Keep the highest-scoring lead per domain; merge source-discovery flags."""
    best: dict[str, Lead] = {}
    for lead in leads:
        old = best.get(lead.domain)
        if old is None:
            best[lead.domain] = lead
        else:
            # Accumulate discovery flags regardless of which score wins
            merged_search  = old.found_by_search  or lead.found_by_search
            merged_catalog = old.found_by_catalog or lead.found_by_catalog
            if lead.reseller_score > old.reseller_score:
                best[lead.domain] = lead
            best[lead.domain].found_by_search  = merged_search
            best[lead.domain].found_by_catalog = merged_catalog
    return sorted(best.values(), key=lambda x: x.reseller_score, reverse=True)


def build_contacts_df(leads: list[Lead]) -> pd.DataFrame:
    """One row per email address per lead.

    Phone is included only when it was paired to that specific contact during
    crawling (email_phones field).  Page-level phones that couldn't be tied to
    a person are intentionally omitted so the contact row stays accurate.
    """
    rows = []
    for lead in leads:
        lead_id     = lead_id_from_url(lead.website)
        emails      = [e.strip() for e in lead.emails.split(",")       if e.strip()] if lead.emails       else []
        titles      = [t.strip() for t in lead.email_titles.split(",") if True]      if lead.email_titles else []
        per_phones  = [p.strip() for p in lead.email_phones.split(",") if True]      if lead.email_phones else []
        per_names   = [n.strip() for n in lead.email_names.split(",")  if True]      if lead.email_names  else []
        base = {
            "lead_id":      lead_id,
            "company":      lead.company,
            "domain":       lead.domain,
            "website":      lead.website,
            "country":      lead.country,       # ISO code  e.g. "NO"
            "country_name": lead.country_name,  # full name e.g. "Norway"
            "priority":     lead.priority,
            "reseller_score": lead.reseller_score,
            "linkedin":     lead.linkedin,
            "contact_page": lead.contact_page,
        }
        if not emails:
            rows.append({**base, "name": "", "email": "", "title": "", "phone": ""})
        for i, email in enumerate(emails):
            phone = per_phones[i] if i < len(per_phones) else ""
            name  = per_names[i]  if i < len(per_names)  else ""
            rows.append({**base, "name": name, "email": email,
                         "title": titles[i] if i < len(titles) else "",
                         "phone": phone})
    return pd.DataFrame(rows)


def autofit_sheet(ws) -> None:
    ws.freeze_panes = "A2"
    for col in ws.columns:
        max_len = min(max(len(str(c.value or "")) for c in col) + 2, 55)
        ws.column_dimensions[col[0].column_letter].width = max_len


def export(leads: list[Lead], outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)

    # Enrich every row with the same slug-based lead_id used everywhere
    rows = [{"lead_id": lead_id_from_url(l.website), **asdict(l)} for l in leads]

    df = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["lead_id"] + list(Lead.__dataclass_fields__.keys())
    )
    df.to_csv(outdir / "agency_leads.csv", index=False, quoting=csv.QUOTE_MINIMAL)

    contacts_df = build_contacts_df(leads)
    contacts_df.to_csv(outdir / "agency_contacts.csv", index=False, quoting=csv.QUOTE_MINIMAL)

    with pd.ExcelWriter(outdir / "agency_leads.xlsx", engine="openpyxl") as writer:
        _sanitize_df(df).to_excel(writer, sheet_name="Leads", index=False)
        _sanitize_df(contacts_df).to_excel(writer, sheet_name="Contacts", index=False)
        summary = pd.DataFrame([
            {"metric": "Total leads",    "value": len(df)},
            {"metric": "A priority",     "value": int((df.get("priority", pd.Series(dtype=str)).astype(str).str.startswith("A")).sum()) if not df.empty else 0},
            {"metric": "With email",     "value": int((df.get("emails", pd.Series(dtype=str)).astype(str).str.len() > 0).sum()) if not df.empty else 0},
            {"metric": "Total contacts", "value": int((contacts_df["email"] != "").sum()) if not contacts_df.empty else 0},
            {"metric": "Generated at",   "value": datetime.now().isoformat(timespec="seconds")},
        ])
        summary.to_excel(writer, sheet_name="Dashboard", index=False)
        qdf = pd.DataFrame({"query": load_lines(Path("config/queries_all.txt"))})
        qdf.to_excel(writer, sheet_name="Queries", index=False)
        autofit_sheet(writer.book["Leads"])
        autofit_sheet(writer.book["Contacts"])

    # JSON: leads with lead_id, plus a separate contacts list
    (outdir / "agency_leads.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (outdir / "agency_contacts.json").write_text(
        json.dumps(contacts_df.to_dict(orient="records"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_existing_leads(output_path: Path) -> list[Lead]:
    """Reload all leads from a previous run's CSV so data is never lost on re-run."""
    csv_path = output_path / "agency_leads.csv"
    if not csv_path.exists():
        return []
    try:
        df = pd.read_csv(csv_path, dtype=str).fillna("")
        df = df.drop(columns=["lead_id"], errors="ignore")
        fields = set(Lead.__dataclass_fields__)
        df = df[[c for c in df.columns if c in fields]]
        leads: list[Lead] = []
        for row in df.to_dict(orient="records"):
            try:
                row["reseller_score"] = int(float(row.get("reseller_score", 0)))
            except (ValueError, TypeError):
                row["reseller_score"] = 0
            leads.append(Lead(**{k: v for k, v in row.items() if k in fields}))
        print(f"Loaded {len(leads)} existing leads from {csv_path}")
        return leads
    except Exception as e:
        print(f"Warning: could not read existing CSV ({e}) -- starting fresh")
        return []
