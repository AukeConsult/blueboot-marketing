"""Lead dataclass + export helpers."""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

import pandas as pd

from utils import load_lines


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


def dedupe_leads(leads: list[Lead]) -> list[Lead]:
    best: dict[str, Lead] = {}
    for lead in leads:
        old = best.get(lead.domain)
        if old is None or lead.reseller_score > old.reseller_score:
            best[lead.domain] = lead
    return sorted(best.values(), key=lambda x: x.reseller_score, reverse=True)


def build_contacts_df(leads: list[Lead]) -> pd.DataFrame:
    """One row per email address per lead."""
    rows = []
    for lead in leads:
        lead_id = hashlib.sha1(lead.domain.encode()).hexdigest()[:10]
        emails  = [e.strip() for e in lead.emails.split(",") if e.strip()] if lead.emails else []
        titles  = [t.strip() for t in lead.email_titles.split(",")] if lead.email_titles else []
        base = {
            "lead_id": lead_id, "company": lead.company, "domain": lead.domain,
            "website": lead.website, "country": lead.country_name,
            "priority": lead.priority, "reseller_score": lead.reseller_score,
            "phones": lead.phones, "linkedin": lead.linkedin,
            "contact_page": lead.contact_page,
        }
        if not emails:
            rows.append({**base, "email": "", "title": ""})
        for i, email in enumerate(emails):
            rows.append({**base, "email": email, "title": titles[i] if i < len(titles) else ""})
    return pd.DataFrame(rows)


def autofit_sheet(ws) -> None:
    ws.freeze_panes = "A2"
    for col in ws.columns:
        max_len = min(max(len(str(c.value or "")) for c in col) + 2, 55)
        ws.column_dimensions[col[0].column_letter].width = max_len


def export(leads: list[Lead], outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    rows = [asdict(l) for l in leads]
    df = pd.DataFrame(rows)
    if not df.empty:
        df.insert(0, "lead_id", [hashlib.sha1(r["domain"].encode()).hexdigest()[:10] for r in rows])
    else:
        df = pd.DataFrame(columns=["lead_id"] + list(Lead.__dataclass_fields__.keys()))
    df.to_csv(outdir / "agency_leads.csv", index=False, quoting=csv.QUOTE_MINIMAL)

    contacts_df = build_contacts_df(leads)
    with pd.ExcelWriter(outdir / "agency_leads.xlsx", engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Leads", index=False)
        contacts_df.to_excel(writer, sheet_name="Contacts", index=False)
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
    (outdir / "agency_leads.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
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
        leads = []
        for row in df.to_dict(orient="records"):
            try:
                row["reseller_score"] = int(float(row.get("reseller_score", 0)))
            except (ValueError, TypeError):
                row["reseller_score"] = 0
            leads.append(Lead(**{k: v for k, v in row.items() if k in fields}))
        print(f"Loaded {len(leads)} existing leads from {csv_path}")
        return leads
    except Exception as e:
        print(f"Warning: could not read existing CSV ({e}) — starting fresh")
        return []
