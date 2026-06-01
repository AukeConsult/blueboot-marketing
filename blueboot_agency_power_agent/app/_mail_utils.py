"""_mail_utils.py — Shared helpers for site_campaign_mail_prepare and lead_campaign_mail_prepare."""
from __future__ import annotations

import json
from pathlib import Path

MAIL_CATALOGUE_DIR = "mailing"

EXAMPLE_BODIES: dict[str, str] = {
    "NO": """<p>Hei {{name}},</p>
<p>Vi oppdaget nettsiden til {{company}} ({{domain}}) og ønsket å ta kontakt.</p>
<p>Hos BlueSearch hjelper vi bedrifter med å bli funnet online. Basert på det vi ser på nettstedet ditt tror vi vi kan hjelpe.</p>
<p>Er dette noe du vil vite mer om?</p>
<p>Vennlig hilsen<br>BlueSearch-teamet</p>""",
    "SE": """<p>Hej {{name}},</p>
<p>Vi hittade {{company}} ({{domain}}) och ville höra av oss.</p>
<p>På BlueSearch hjälper vi företag att synas online. Utifrån vad vi ser på er webbplats tror vi vi kan hjälpa er.</p>
<p>Är det något du vill veta mer om?</p>
<p>Med vänliga hälsningar<br>BlueSearch-teamet</p>""",
    "DK": """<p>Hej {{name}},</p>
<p>Vi fandt {{company}} ({{domain}}) og ville gerne tage kontakt.</p>
<p>Hos BlueSearch hjælper vi virksomheder med at blive fundet online.</p>
<p>Er det noget du vil høre mere om?</p>
<p>Med venlig hilsen<br>BlueSearch-teamet</p>""",
    "FI": """<p>Hei {{name}},</p>
<p>Löysimme verkkosivustonne {{domain}} ja halusimme ottaa yhteyttä.</p>
<p>BlueSearchissa autamme yrityksiä löytymään verkossa.</p>
<p>Haluatko kuulla lisää?</p>
<p>Ystävällisin terveisin<br>BlueSearch-tiimi</p>""",
    "_default": """<p>Hi {{name}},</p>
<p>We came across {{company}} ({{domain}}) and wanted to reach out.</p>
<p>At BlueSearch we help businesses get found online. Based on what we see on your site we think we can help.</p>
<p>Would you like to know more?</p>
<p>Best regards<br>The BlueSearch team</p>""",
}

SUBJECT_DEFAULTS: dict[str, str] = {
    "NO": "Hei fra BlueSearch — vi fant deg online",
    "SE": "Hej från BlueSearch — vi hittade dig online",
    "DK": "Hej fra BlueSearch — vi fandt dig online",
    "FI": "Hei BlueSearchilta — löysimme sinut verkosta",
}


def project_root() -> Path:
    return Path(__file__).parent.parent


def mail_dir(catalogue_key: str) -> Path:
    return project_root() / MAIL_CATALOGUE_DIR / catalogue_key / "mails"


def subject_file_path(catalogue_key: str) -> Path:
    return project_root() / MAIL_CATALOGUE_DIR / catalogue_key / "subject.json"


def scaffold_mail_catalogue(catalogue_key: str, countries: list[str]) -> None:
    """Create mailing/{catalogue_key}/ with example files if missing."""
    mails_dir    = mail_dir(catalogue_key)
    subject_path = subject_file_path(catalogue_key)
    mails_dir.mkdir(parents=True, exist_ok=True)
    created = []

    for country in countries:
        fname = mails_dir / f"body_{country}.html"
        if not fname.exists():
            body = EXAMPLE_BODIES.get(country, EXAMPLE_BODIES["_default"])
            fname.write_text(body, encoding="utf-8")
            created.append(str(fname.relative_to(project_root())))

    fallback = mails_dir / "body.html"
    if not fallback.exists():
        fallback.write_text(EXAMPLE_BODIES["_default"], encoding="utf-8")
        created.append(str(fallback.relative_to(project_root())))

    if not subject_path.exists():
        subjects = {c: SUBJECT_DEFAULTS.get(c, "Hello from BlueSearch") for c in countries}
        subject_path.write_text(
            json.dumps(subjects, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8"
        )
        created.append(str(subject_path.relative_to(project_root())))

    if created:
        print(f"  [mail-prepare] Created mail catalogue for '{catalogue_key}':")
        for f in created:
            print(f"    {f}")
    else:
        print(f"  [mail-prepare] Mail catalogue exists: {MAIL_CATALOGUE_DIR}/{catalogue_key}/")


def resolve_body(country: str, body_file: str | None, body_dir: str | None) -> str:
    if body_dir:
        p = Path(body_dir)
        for name in [f"body_{country}.html", f"body_{country}.txt",
                     f"body_{country.lower()}.html", f"body_{country.lower()}.txt",
                     "body.html", "body.txt"]:
            candidate = p / name
            if candidate.exists():
                return candidate.read_text(encoding="utf-8")
    if body_file:
        p = Path(body_file)
        if p.exists():
            return p.read_text(encoding="utf-8")
    return ""


def resolve_subject(country: str, default_subject: str, subject_map: dict[str, str]) -> str:
    return subject_map.get(country) or subject_map.get(country.lower()) or default_subject


def personalise(body: str, subject: str, contact: dict, lead: dict) -> tuple[str, str]:
    """Substitute {{placeholders}} in body and subject."""
    replacements = {
        "{{name}}":        ((contact.get("name") or "").split() or [""])[0],
        "{{full_name}}":   contact.get("name") or "",
        "{{email}}":       contact.get("email") or "",
        "{{occupation}}":  contact.get("occupation") or contact.get("title") or "",
        "{{company}}":     contact.get("company") or lead.get("company") or "",
        "{{domain}}":      contact.get("domain") or lead.get("domain") or "",
        "{{website}}":     contact.get("website") or lead.get("website") or "",
        "{{country}}":     contact.get("country") or lead.get("country") or "",
        "{{ai_sector}}":   lead.get("ai_sector") or lead.get("categories") or "",
        "{{ai_summary}}":  lead.get("ai_summary") or lead.get("description") or "",
    }
    for placeholder, value in replacements.items():
        body    = body.replace(placeholder, value)
        subject = subject.replace(placeholder, value)
    return body, subject
