from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse, parse_qs, unquote

import pandas as pd
import phonenumbers
import requests
import tldextract
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from rapidfuzz import fuzz

from send_mail import make_outreach

USER_AGENT = "BlueBootLeadAgent/1.1 (+https://blueboot.ai)"
BING_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+\-]+@[a-zA-Z0-9\-]+(?:\.[a-zA-Z0-9\-]+)+")
GENERIC_PHONE_RE = re.compile(r"(?:(?:\+|00)\d{1,3}\s*)?(?:\d[\s().-]?){7,15}")
DEFAULT_COUNTRIES = ["NO"]
COUNTRY_CONFIG_PATH = Path("config/countries.json")

TECH_SIGNATURES = {
    # --- WordPress ecosystem ---
    "WordPress":        ["wp-content", "wp-includes", "wordpress"],
    "WooCommerce":      ["woocommerce", "wc-blocks", "wp-content/plugins/woocommerce"],
    "Elementor":        ["elementor-frontend", "elementor/assets"],
    "Divi":             ["et-pb-", "divi/js", "extra/css"],

    # --- Enterprise / .NET CMS (very common with Scandinavian agencies) ---
    "Episerver":        ["episerver", "epi-", "/EPiServer/", "episerver.js"],
    "Optimizely CMS":   ["optimizely", "optimizelycms", "optly"],
    "Umbraco":          ["umbraco", "/umbraco/", "umbraco.js"],
    "Sitecore":         ["sitecore", "/-/media/", "sitecore/shell"],
    "Kentico":          ["kentico", "cmsdesk", "/CMSPages/"],
    "TYPO3":            ["typo3", "typo3conf", "typo3/sysext"],
    "DotNetNuke":       ["dnn", "dotnetnuke", "/desktopmodules/"],

    # --- E-commerce platforms ---
    "Shopify":          ["cdn.shopify.com", "shopify"],
    "Magento":          ["magento", "mage/", "Magento_"],
    "PrestaShop":       ["prestashop", "/modules/prestashop", "presta-"],
    "WooCommerce":      ["woocommerce", "wc-blocks"],
    "Shopware":         ["shopware", "sw-plugin"],
    "BigCommerce":      ["bigcommerce", "bc-sf-filter"],
    "OpenCart":         ["opencart", "catalog/view/theme"],

    # --- Headless / modern CMS ---
    "Contentful":       ["contentful", "ctfassets.net"],
    "Sanity":           ["sanity.io", "cdn.sanity.io"],
    "Storyblok":        ["storyblok", "a.storyblok.com"],
    "Prismic":          ["prismic.io", "cdn.prismic.io"],
    "Strapi":           ["strapi", "/api/strapi"],
    "Craft CMS":        ["craftcms", "craft-cms"],
    "Ghost":            ["ghost.io", "/ghost/", "ghost-theme"],

    # --- Website builders / SaaS ---
    "Webflow":          ["webflow", "assets.website-files.com"],
    "Squarespace":      ["squarespace"],
    "Wix":              ["wixstatic", "wix.com"],
    "Framer":           ["framer.com", "framerusercontent.com"],

    # --- Marketing / CRM platforms ---
    "HubSpot":          ["hs-scripts", "hubspot"],
    "Salesforce":       ["salesforce", "force.com", "sfdcstatic"],
    "Marketo":          ["marketo", "munchkin.js"],
    "ActiveCampaign":   ["activecampaign", "trackcmp.net"],
}

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
    outreach_subject: str = ""
    outreach_email: str = ""
    status: str = "New"
    country: str = ""
    country_name: str = ""
    notes: str = ""
    crawled_at: str = ""


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}"


def domain_of(url: str) -> str:
    ext = tldextract.extract(url)
    return ".".join(part for part in [ext.domain, ext.suffix] if part)


def company_from_domain(domain: str) -> str:
    name = domain.split(".")[0].replace("-", " ").replace("_", " ")
    return " ".join(w.capitalize() for w in name.split())


def load_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [x.strip() for x in path.read_text(encoding="utf-8").splitlines() if x.strip() and not x.strip().startswith("#")]


def load_country_configs(path: Path = COUNTRY_CONFIG_PATH) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def selected_countries(value: str, configs: dict) -> list[str]:
    if value.upper() == "ALL":
        return list(configs.keys())
    result = [x.strip().upper() for x in value.split(",") if x.strip()]
    return [x for x in result if x in configs]


def fetch(url: str, timeout=15, accept_language="en;q=0.8") -> str:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": accept_language}
    r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    if "text" not in r.headers.get("content-type", "") and "html" not in r.headers.get("content-type", ""):
        return ""
    return r.text[:2_000_000]


def bing_search(query: str, max_results: int, exclude_domains: set[str] | None = None) -> list[str]:
    """Search Bing via RSS feed — returns clean XML with actual result URLs."""
    import xml.etree.ElementTree as ET

    MAX_EXCLUSIONS = 20
    q = query
    if exclude_domains:
        exclusions = list(exclude_domains)[:MAX_EXCLUSIONS]
        q = query + " " + " ".join(f"-site:{d}" for d in exclusions)

    urls: list[str] = []
    seen: set[str] = set()
    page = 1
    PAGE_SIZE = 100  # ask for 100; Bing will return as many as it has (typically 50)

    while len(urls) < max_results:
        first = (page - 1) * PAGE_SIZE + 1
        params = {"q": q, "format": "rss", "count": PAGE_SIZE, "first": first}
        try:
            resp = requests.get(
                "https://www.bing.com/search",
                params=params,
                headers={
                    "User-Agent": BING_USER_AGENT,
                    "Accept": "application/rss+xml, text/xml, */*",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=20,
            )
            root = ET.fromstring(resp.text)
        except Exception as e:
            print(f"  [Bing] RSS parse error: {e}")
            break

        items = root.findall(".//item")
        if not items:
            break

        added = 0
        for item in items:
            link = item.find("link")
            if link is not None and link.text and link.text.startswith("http"):
                url = link.text.strip()
                if url not in seen:
                    urls.append(url)
                    seen.add(url)
                    added += 1
        if added == 0:
            break
        page += 1

    return urls[:max_results]

def google_cse_search(query: str, max_results: int) -> list[str]:
    key = os.getenv("GOOGLE_API_KEY")
    cse = os.getenv("GOOGLE_CSE_ID")
    if not key or not cse:
        return []
    urls = []
    for start in range(1, max_results + 1, 10):
        params = {"key": key, "cx": cse, "q": query, "num": min(10, max_results - len(urls)), "start": start}
        try:
            r = requests.get("https://www.googleapis.com/customsearch/v1", params=params, timeout=20)
            r.raise_for_status()
            data = r.json()
            urls += [item["link"] for item in data.get("items", []) if item.get("link")]
        except Exception:
            break
        if len(urls) >= max_results:
            break
    return urls[:max_results]


def clean_search_url(url: str) -> str:
    if "bing.com/ck/a" in url:
        qs = parse_qs(urlparse(url).query)
        for key in ("u", "url"):
            if key in qs:
                val = qs[key][0]
                if val.startswith("a1"):
                    val = val[2:]
                    try:
                        padding = (4 - len(val) % 4) % 4
                        val = base64.b64decode(val + "=" * padding).decode("utf-8")
                    except Exception:
                        pass
                return unquote(val)
    return url


PRODUCT_PAGE_PATTERNS = [
    "/c/", "/category/", "/katalog/", "/kategori/",
    "/product/", "/produkt/", "/products/", "/produkter/",
    "/shop/", "/store/", "/butikk/",
    "/p/", "/item/", "/items/",
    "/blogg/", "/blog/", "/news/", "/nyheter/",
    "/article/", "/artikkel/",
]

def is_product_or_content_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(pat in path for pat in PRODUCT_PAGE_PATTERNS)


def is_blocked(domain: str, blocklist: set[str]) -> bool:
    """Return True if domain matches any blocklist entry (exact, subdomain, or wildcard)."""
    from fnmatch import fnmatch
    domain = domain.lower()
    for entry in blocklist:
        if "*" in entry:
            if fnmatch(domain, entry):
                return True
        else:
            if domain == entry or domain.endswith("." + entry):
                return True
    return False


def allowed_domain(domain: str, blocklist: set[str], countries: list[str], configs: dict) -> bool:
    if not domain or is_blocked(domain, blocklist):
        return False
    if domain in {"blueboot.ai"}:
        return True
    return country_for_domain(domain, countries, configs) is not None


def country_for_domain(domain: str, countries: list[str], configs: dict) -> str | None:
    domain_l = domain.lower()
    for code in countries:
        for tld in configs.get(code, {}).get("tlds", []):
            if domain_l.endswith(tld):
                return code
    return None


def extract_meta(soup: BeautifulSoup) -> tuple[str, str]:
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    desc = ""
    tag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", property="og:description")
    if tag and tag.get("content"):
        desc = tag["content"].strip()
    return title[:250], desc[:500]


def visible_text(soup: BeautifulSoup) -> str:
    for s in soup(["script", "style", "noscript"]):
        s.extract()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:120_000]


TITLE_KEYWORDS = re.compile(
    r"\b(ceo|cto|coo|cmo|cfo|vp|partner|founder|co-founder|owner|president|"
    r"director|head of|lead|manager|chief|principal|consultant|advisor|"
    r"daglig leder|administrerende direkt[oø]r|gründer|sjef|leder|direkt[oø]r|"
    r"vd|verkst[äa]llande direkt[öo]r|"
    r"gesch[äa]ftsf[üu]hrer|inhaber|leiter|gesch[äa]ftsleitung|"
    r"directeur|g[eé]rant|fondateur|responsable|chef de projet|"
    r"director|gerente|fundador|responsable|jefe de proyecto)\b",
    re.IGNORECASE
)


def extract_contacts(html: str, text: str) -> dict[str, str]:
    """Return {email: title} — title is best-effort from surrounding text."""
    combined = html + " " + text
    raw_emails = EMAIL_RE.findall(combined)
    contacts: dict[str, str] = {}
    for e in raw_emails:
        e = e.strip(".,;:()[]<>").lower()
        if any(e.endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"]):
            continue
        if e in contacts:
            continue
        # Search ±300 chars around the email for a job title
        title = ""
        idx = combined.lower().find(e)
        if idx != -1:
            window = combined[max(0, idx - 300): idx + 300]
            m = TITLE_KEYWORDS.search(window)
            if m:
                # Grab up to 60 chars starting from the match for context
                title = window[m.start(): m.start() + 60].strip()
                title = re.sub(r"\s+", " ", title)[:60]
        contacts[e] = title
    return contacts


def extract_phones(text: str, country="NO") -> set[str]:
    phones = set()
    for m in GENERIC_PHONE_RE.findall(text):
        raw = re.sub(r"[^+0-9]", "", m)
        try:
            num = phonenumbers.parse(raw, country)
            if phonenumbers.is_valid_number(num):
                phones.add(phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.INTERNATIONAL))
        except Exception:
            pass
    return phones


def extract_links(base_url: str, soup: BeautifulSoup) -> tuple[list[str], str, str]:
    links, contact_page, linkedin = [], "", ""
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        txt = a.get_text(" ", strip=True).lower()
        links.append(href)
        if not contact_page and any(x in href.lower() or x in txt for x in ["kontakt", "contact", "om-oss", "about"]):
            contact_page = href
        if "linkedin.com/company" in href.lower() and not linkedin:
            linkedin = href
    return links, contact_page, linkedin


def detect_tech(html: str, soup: BeautifulSoup) -> set[str]:
    low = html.lower()
    tech = set()
    for name, sigs in TECH_SIGNATURES.items():
        if any(sig.lower() in low for sig in sigs):
            tech.add(name)
    gen = soup.find("meta", attrs={"name": "generator"})
    if gen and gen.get("content"):
        tech.add(gen["content"][:80])
    return tech


def categorize(text: str, html: str, country_cfg: dict) -> tuple[set[str], list[str], int]:
    hay = (text + " " + html[:250000]).lower()
    cats, reasons = set(), []
    score = 0
    weights = {"web_agency": 25, "wordpress": 25, "seo": 18, "communication": 18, "public_sector": 10, "ai_interest": 8}
    for cat, kws in country_cfg.get("keywords", {}).items():
        hits = [kw for kw in kws if kw.lower() in hay]
        if hits:
            cats.add(cat)
            score += weights[cat]
            reasons.append(f"{cat}: " + ", ".join(hits[:4]))
    if any(x in hay for x in country_cfg.get("service_words", ["services", "customers", "clients", "case"])):
        score += 8
        reasons.append("has services/customers/cases language")
    if any(x in hay for x in country_cfg.get("support_words", ["support", "hosting", "maintenance"])):
        score += 6
        reasons.append("offers maintenance/support")
    # Boost confirmed agency language
    agency_hits = [x for x in country_cfg.get("agency_words", []) if x.lower() in hay]
    if agency_hits:
        score += min(len(agency_hits) * 10, 20)
        reasons.append("agency language: " + ", ".join(agency_hits[:3]))
    # Penalise clearly non-agency businesses
    neg_hits = [kw for kw in country_cfg.get("negative_keywords", []) if kw.lower() in hay]
    if neg_hits:
        penalty = min(len(neg_hits) * 30, 90)
        score -= penalty
        reasons.append(f"NON-AGENCY penalty ({', '.join(neg_hits[:3])}): -{penalty}")
    return cats, reasons, max(min(score, 100), 0)


def priority(score: int) -> str:
    if score >= 75: return "A - High fit"
    if score >= 55: return "B - Good fit"
    if score >= 35: return "C - Maybe"
    return "D - Low fit"


def angle(cats: set[str], tech: set[str]) -> str:
    # Enterprise CMS — high-value, content-heavy installs
    if any(t in tech for t in ("Episerver", "Optimizely CMS")):
        return "Position BlueSearch as an AI search layer on top of Episerver/Optimizely — no rebuild needed, instant upgrade for content-heavy customer sites."
    if "Umbraco" in tech:
        return "Offer BlueSearch as a plug-in AI search add-on for their Umbraco customer base — great fit for large content and documentation sites."
    if "Sitecore" in tech:
        return "Sitecore agencies can offer BlueSearch as an AI search upgrade — positions them ahead of competitors on search experience."
    if any(t in tech for t in ("TYPO3", "Kentico", "DotNetNuke")):
        return f"BlueSearch adds modern AI search to {next(t for t in tech if t in ('TYPO3','Kentico','DotNetNuke'))} sites — a recurring managed service with no core changes needed."

    # E-commerce
    if any(t in tech for t in ("Shopify", "Magento", "WooCommerce", "PrestaShop", "Shopware")):
        platform = next(t for t in tech if t in ("Shopify", "Magento", "WooCommerce", "PrestaShop", "Shopware"))
        return f"Offer BlueSearch as an AI product-search add-on for {platform} stores — boosts conversion and can be sold as a recurring service."

    # Headless / modern CMS
    if any(t in tech for t in ("Contentful", "Sanity", "Storyblok", "Prismic")):
        return "BlueSearch integrates via API with headless CMS setups — pitch it as the missing AI search layer for their Jamstack/headless customers."

    # WordPress ecosystem
    if "WordPress" in tech or "wordpress" in cats:
        return "Offer BlueSearch as a WordPress/WooCommerce AI-search add-on for their customer base."

    # SEO / communication
    if "seo" in cats:
        return "Position BlueSearch as AI visibility + better on-site discovery for SEO clients."
    if "communication" in cats or "public_sector" in cats:
        return "Focus on public-information sites: help visitors find answers across pages, PDFs and articles."

    return "General reseller angle: add AI-powered search to existing customer websites without rebuilding them."


def crawl_site(url: str, source_query: str, max_pages: int, delay: float, country_code: str, country_cfg: dict) -> Lead | None:
    website = normalize_url(url)
    dom = domain_of(website)
    seen, queue = set(), [website]
    all_text, all_html = "", ""
    contacts: dict[str, str] = {}
    phones, tech = set(), set()
    title = desc = contact_page = linkedin = ""

    while queue and len(seen) < max_pages:
        page = queue.pop(0)
        if page in seen or domain_of(page) != dom:
            continue
        seen.add(page)
        try:
            html = fetch(page, accept_language=country_cfg.get("accept_language", "en;q=0.8"))
        except Exception:
            continue
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        if not title:
            title, desc = extract_meta(soup)
        text = visible_text(soup)
        all_text += " " + text
        all_html += " " + html[:300000]
        contacts.update(extract_contacts(html, text))
        phones |= extract_phones(text, country_cfg.get("phone_region", country_code))
        tech |= detect_tech(html, soup)
        links, cp, li = extract_links(page, soup)
        contact_page = contact_page or cp
        linkedin = linkedin or li
        for l in links:
            low = l.lower().split("#")[0]
            if domain_of(low) == dom and any(x in low for x in country_cfg.get("contact_words", ["contact", "about", "services", "case"])):
                if low not in seen and low not in queue:
                    queue.append(low)
        time.sleep(delay)

    if not all_text and not all_html:
        return None
    cats, reasons, score = categorize(all_text, all_html, country_cfg)
    if score < 20 and fuzz.partial_ratio("digitalbyrå webdesign wordpress seo", all_text[:5000].lower()) < 35:
        return None
    lead_angle = angle(cats, tech)
    subject, email_body = make_outreach(company_from_domain(dom), dom, cats, lead_angle, country_cfg.get("name", country_code))
    return Lead(
        company=company_from_domain(dom), domain=dom, website=website, source_query=source_query,
        title=title, description=desc,
        emails=", ".join(sorted(contacts.keys())),
        email_titles=", ".join(contacts.get(e, "") for e in sorted(contacts.keys())),
        phones=", ".join(sorted(phones)),
        contact_page=contact_page, linkedin=linkedin, detected_tech=", ".join(sorted(tech)),
        categories=", ".join(sorted(cats)), reseller_score=score, priority=priority(score), reasons="; ".join(reasons),
        suggested_angle=lead_angle, outreach_subject=subject, outreach_email=email_body,
        country=country_code, country_name=country_cfg.get("name", country_code),
        crawled_at=datetime.now(timezone.utc).isoformat(timespec="seconds")
    )


def dedupe_leads(leads: list[Lead]) -> list[Lead]:
    best = {}
    for lead in leads:
        old = best.get(lead.domain)
        if old is None or lead.reseller_score > old.reseller_score:
            best[lead.domain] = lead
    return sorted(best.values(), key=lambda x: x.reseller_score, reverse=True)


def build_contacts_df(leads: list[Lead]) -> pd.DataFrame:
    """One row per email address per lead, with title when available."""
    contact_rows = []
    for lead in leads:
        lead_id = hashlib.sha1(lead.domain.encode()).hexdigest()[:10]
        emails = [e.strip() for e in lead.emails.split(",") if e.strip()] if lead.emails else []
        titles = [t.strip() for t in lead.email_titles.split(",") if True] if lead.email_titles else []
        if not emails:
            contact_rows.append({
                "lead_id": lead_id,
                "email": "",
                "title": "",
                "company": lead.company,
                "domain": lead.domain,
                "website": lead.website,
                "country": lead.country_name,
                "priority": lead.priority,
                "reseller_score": lead.reseller_score,
                "phones": lead.phones,
                "linkedin": lead.linkedin,
                "contact_page": lead.contact_page,
                "outreach_subject": lead.outreach_subject,
                "outreach_email": lead.outreach_email,
            })
        for i, email in enumerate(emails):
            contact_rows.append({
                "lead_id": lead_id,
                "email": email,
                "title": titles[i] if i < len(titles) else "",
                "company": lead.company,
                "domain": lead.domain,
                "website": lead.website,
                "country": lead.country_name,
                "priority": lead.priority,
                "reseller_score": lead.reseller_score,
                "phones": lead.phones,
                "linkedin": lead.linkedin,
                "contact_page": lead.contact_page,
                "outreach_subject": lead.outreach_subject,
                "outreach_email": lead.outreach_email,
            })
    return pd.DataFrame(contact_rows)


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
            {"metric": "Total leads", "value": len(df)},
            {"metric": "A priority", "value": int((df.get("priority", pd.Series(dtype=str)).astype(str).str.startswith("A")).sum()) if not df.empty else 0},
            {"metric": "With email", "value": int((df.get("emails", pd.Series(dtype=str)).astype(str).str.len() > 0).sum()) if not df.empty else 0},
            {"metric": "Total contacts", "value": int((contacts_df["email"] != "").sum()) if not contacts_df.empty else 0},
            {"metric": "Generated at", "value": datetime.now().isoformat(timespec="seconds")},
        ])
        summary.to_excel(writer, sheet_name="Dashboard", index=False)
        qdf = pd.DataFrame({"query": load_lines(Path("config/queries_all.txt"))})
        qdf.to_excel(writer, sheet_name="Queries", index=False)
        autofit_sheet(writer.book["Leads"])
        autofit_sheet(writer.book["Contacts"])
    (outdir / "agency_leads.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def load_queries_for_countries(countries: list[str], explicit_queries: str | None = None) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    if explicit_queries:
        for q in load_lines(Path(explicit_queries)):
            pairs.append((q, "AUTO"))
        return pairs
    for code in countries:
        path = Path(f"config/queries_{code}.txt")
        for q in load_lines(path):
            pairs.append((q, code))
    return pairs


def load_existing_domains(output_path: Path) -> set[str]:
    """Read the Leads sheet from an existing Excel export and return all domains already crawled."""
    xlsx = output_path / "agency_leads.xlsx"
    if not xlsx.exists():
        return set()
    try:
        df = pd.read_excel(xlsx, sheet_name="Leads", usecols=["domain"], dtype=str)
        domains = set(df["domain"].dropna().str.strip().str.lower())
        print(f"Loaded {len(domains)} already-crawled domains from {xlsx}")
        return domains
    except Exception as e:
        print(f"Warning: could not read existing Excel ({e}) — starting fresh")
        return set()


def run() -> None:
    import sys
    sys.stdout.reconfigure(line_buffering=True)
    print("BlueBoot Lead Agent starting...", flush=True)
    load_dotenv()
    parser = argparse.ArgumentParser(description="BlueBoot agency lead finder")
    parser.add_argument("--countries", default=os.getenv("COUNTRIES", "NO"),
                        help="Comma separated country codes: NO,SE,DK,DE,UK,FR,ES or ALL")
    parser.add_argument("--queries", default=os.getenv("QUERIES_FILE", ""),
                        help="Optional custom query file. If empty, loads config/queries_<COUNTRY>.txt")
    parser.add_argument("--output", default="output")
    parser.add_argument("--max-results", type=int, default=int(os.getenv("MAX_RESULTS_PER_QUERY", "1000")))
    parser.add_argument("--max-pages", type=int, default=int(os.getenv("MAX_PAGES_PER_SITE", "6")))
    parser.add_argument("--max-country", type=int, default=int(os.getenv("MAX_LEADS_PER_COUNTRY", "500")),
                        help="Max new leads to crawl per country (0 = unlimited). "
                             "Keeps running queries until the cap is reached or 3 consecutive queries yield nothing new.")
    parser.add_argument("--give-up-after", type=int, default=3,
                        help="Give up on a country after this many consecutive queries with zero new candidates (default 3)")
    parser.add_argument("--delay", type=float, default=float(os.getenv("REQUEST_DELAY_SECONDS", "1.0")))
    args = parser.parse_args()

    configs = load_country_configs()
    countries = selected_countries(args.countries, configs) or DEFAULT_COUNTRIES
    query_pairs = load_queries_for_countries(countries, args.queries or None)
    blocklist = set(load_lines(Path("config/blocklist_domains.txt")))

    # --- Skip domains already present in a previous run's Excel ---
    existing_domains = load_existing_domains(Path(args.output))
    seen_domains: set[str] = set(existing_domains)

    # --- Build per-country query queues ---
    from collections import defaultdict
    queues: dict[str, list[str]] = defaultdict(list)
    for q, c in query_pairs:
        queues[c].append(q)

    # Per-country state
    country_leads: dict[str, int] = {c: 0 for c in countries}   # leads found this run
    country_streak: dict[str, int] = {c: 0 for c in countries}  # consecutive queries with 0 new candidates
    country_done: set[str] = set()

    all_leads: list[Lead] = []
    total_queries_run = 0

    print(f"Countries: {', '.join(countries)}")
    if args.max_country:
        print(f"Target: {args.max_country} leads per country  |  Give up after {args.give_up_after} empty queries in a row")
    else:
        print(f"No per-country cap  |  Give up after {args.give_up_after} empty queries in a row")

    # Round-robin across countries until all are done
    while len(country_done) < len(countries):
        made_progress = False
        for code in countries:
            if code in country_done:
                continue

            # Check cap
            if args.max_country and country_leads[code] >= args.max_country:
                print(f"\n[{code}] Target of {args.max_country} leads reached.")
                country_done.add(code)
                continue

            # Check query queue
            if not queues[code]:
                print(f"\n[{code}] No more queries to run — giving up.")
                country_done.add(code)
                continue

            query = queues[code].pop(0)
            total_queries_run += 1
            leads_before = country_leads[code]
            print(f"\n[{code} | leads={country_leads[code]} | streak={country_streak[code]}] Query: {query}")

            # Search — pass seen_domains so Bing excludes already-known sites
            google_urls = google_cse_search(query, args.max_results)
            if google_urls:
                print(f"  Google: {len(google_urls)} results")
                urls = google_urls
   
            else:
                bing_urls = bing_search(query, args.max_results, exclude_domains=seen_domains)
                print(f"  Bing fallback: {len(bing_urls)} results")
                urls = bing_urls

            # Filter candidates
            new_candidates: list[tuple[str, str]] = []
            for raw in urls:
                url = clean_search_url(raw)
                dom = domain_of(url)
                detected_country = country_for_domain(dom, countries, configs) or code
                is_product = is_product_or_content_url(url)
                if is_product:
                    parsed = urlparse(url)
                    url = f"{parsed.scheme}://{parsed.netloc}/"
                already_done = dom in existing_domains
                dup = dom in seen_domains
                if allowed_domain(dom, blocklist, countries, configs) and not dup and detected_country == code:
                    new_candidates.append((url, query))
                    seen_domains.add(dom)

            print(f"  -> {len(new_candidates)} new candidates to crawl")

            # Crawl immediately
            for url, q in new_candidates:
                if args.max_country and country_leads[code] >= args.max_country:
                    print(f"  Cap reached mid-batch — stopping {code}")
                    break
                print(f"  Crawling {url}")
                lead = crawl_site(url, q, args.max_pages, args.delay, code, configs.get(code, {}))
                if lead:
                    has_email = "yes" if lead.emails else "no"
                    print(f"    -> {lead.priority} score={lead.reseller_score} email={has_email}")
                    all_leads.append(lead)
                    country_leads[code] += 1
                    # Save after every lead so progress survives crashes
                    export(dedupe_leads(all_leads), Path(args.output))
                time.sleep(args.delay)

            # Update streak: reset if we found new URLs, else increment
            if new_candidates:
                country_streak[code] = 0
            else:
                country_streak[code] += 1
                print(f"  [{code}] Nothing new — streak {country_streak[code]}/{args.give_up_after}")
                if country_streak[code] >= args.give_up_after:
                    print(f"  [{code}] Giving up after {args.give_up_after} empty queries in a row.")
                    country_done.add(code)

            made_progress = True

        if not made_progress:
            break  # all countries done in this pass

    print(f"\n{'='*60}")
    print(f"Finished. Ran {total_queries_run} queries total.")
    for code in countries:
        status = "done" if code in country_done else "running"
        print(f"  {code}: {country_leads[code]} new leads  ({status})")

    all_leads = dedupe_leads(all_leads)
    export(all_leads, Path(args.output))
    print(f"Exported {len(all_leads)} leads to {args.output}/agency_leads.xlsx")


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        import traceback, sys
        traceback.print_exc()
        sys.exit(1)
