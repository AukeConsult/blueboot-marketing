from __future__ import annotations

import argparse
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

USER_AGENT = "BlueBootLeadAgent/1.1 (+https://blueboot.ai)"
EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+\-]+@[a-zA-Z0-9\-]+(?:\.[a-zA-Z0-9\-]+)+")
GENERIC_PHONE_RE = re.compile(r"(?:(?:\+|00)\d{1,3}\s*)?(?:\d[\s().-]?){7,15}")
DEFAULT_COUNTRIES = ["NO"]
COUNTRY_CONFIG_PATH = Path("config/countries.json")

TECH_SIGNATURES = {
    "WordPress": ["wp-content", "wp-includes", "wordpress"],
    "WooCommerce": ["woocommerce", "wc-blocks", "wp-content/plugins/woocommerce"],
    "Webflow": ["webflow", "assets.website-files.com"],
    "Shopify": ["cdn.shopify.com", "shopify"],
    "Squarespace": ["squarespace"],
    "Wix": ["wixstatic", "wix.com"],
    "HubSpot": ["hs-scripts", "hubspot"],
    "Sanity": ["sanity.io", "cdn.sanity.io"],
    "Craft CMS": ["craftcms", "craft-cms"]
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


def bing_search(query: str, max_results: int) -> list[str]:
    url = "https://www.bing.com/search"
    params = {"q": query, "count": min(max_results, 50)}
    try:
        html = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=20).text
    except Exception:
        return []
    soup = BeautifulSoup(html, "lxml")
    urls: list[str] = []
    for a in soup.select("li.b_algo h2 a, h2 a"):
        href = a.get("href", "")
        if href.startswith("http"):
            urls.append(href)
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
                return unquote(val)
    return url


def allowed_domain(domain: str, blocklist: set[str], countries: list[str], configs: dict) -> bool:
    if not domain or domain in blocklist:
        return False
    if any(domain.endswith("." + b) or domain == b for b in blocklist):
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


def extract_emails(html: str, text: str) -> set[str]:
    emails = set(EMAIL_RE.findall(html + " " + text))
    cleaned = set()
    for e in emails:
        e = e.strip(".,;:()[]<>").lower()
        if not any(e.endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"]):
            cleaned.add(e)
    return cleaned


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
    # Boost likely agencies with services language
    if any(x in hay for x in country_cfg.get("service_words", ["services", "customers", "clients", "case"])): 
        score += 8
        reasons.append("has services/customers/cases language")
    if any(x in hay for x in country_cfg.get("support_words", ["support", "hosting", "maintenance"])):
        score += 6
        reasons.append("offers maintenance/support")
    return cats, reasons, min(score, 100)


def priority(score: int) -> str:
    if score >= 75: return "A - High fit"
    if score >= 55: return "B - Good fit"
    if score >= 35: return "C - Maybe"
    return "D - Low fit"


def angle(cats: set[str], tech: set[str]) -> str:
    if "wordpress" in cats or "WordPress" in tech:
        return "Offer BlueSearch as a WordPress/WooCommerce AI-search add-on for their customer base."
    if "seo" in cats:
        return "Position BlueSearch as AI visibility + better on-site discovery for SEO clients."
    if "communication" in cats or "public_sector" in cats:
        return "Focus on public-information sites: help visitors find answers across pages, PDFs and articles."
    return "General reseller angle: add AI-powered search to existing customer websites without rebuilding them."


def make_outreach(company: str, domain: str, cats: set[str], lead_angle: str, country_name: str) -> tuple[str, str]:
    subject = f"AI search add-on for {company}'s website customers"
    email = f"""Hi {company},

I noticed you work with websites and digital customer communication in {country_name}. We build BlueSearch, an AI-powered search layer that can be added to existing websites so visitors can ask questions and get answers with source links.

Why this may fit your customers:
- easy add-on for existing sites
- useful for content-heavy websites, WordPress/WooCommerce, public information and documentation
- can be sold as a recurring managed service

Suggested angle for {domain}: {lead_angle}

Would it be useful if I sent a short demo showing how it works on a real website?

Best regards,
Leif Auke
BlueBoot R&D AS
https://blueboot.ai
"""
    return subject, email


def crawl_site(url: str, source_query: str, max_pages: int, delay: float, country_code: str, country_cfg: dict) -> Lead | None:
    website = normalize_url(url)
    dom = domain_of(website)
    seen, queue = set(), [website]
    all_text, all_html = "", ""
    emails, phones, tech = set(), set(), set()
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
        emails |= extract_emails(html, text)
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
        title=title, description=desc, emails=", ".join(sorted(emails)), phones=", ".join(sorted(phones)),
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


def export(leads: list[Lead], outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    rows = [asdict(l) for l in leads]
    df = pd.DataFrame(rows)
    if not df.empty:
        df.insert(0, "lead_id", [hashlib.sha1(r["domain"].encode()).hexdigest()[:10] for r in rows])
    else:
        df = pd.DataFrame(columns=["lead_id"] + list(Lead.__dataclass_fields__.keys()))
    df.to_csv(outdir / "agency_leads.csv", index=False, quoting=csv.QUOTE_MINIMAL)
    with pd.ExcelWriter(outdir / "agency_leads.xlsx", engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Leads", index=False)
        summary = pd.DataFrame([
            {"metric": "Total leads", "value": len(df)},
            {"metric": "A priority", "value": int((df.get("priority", pd.Series(dtype=str)).astype(str).str.startswith("A")).sum()) if not df.empty else 0},
            {"metric": "With email", "value": int((df.get("emails", pd.Series(dtype=str)).astype(str).str.len() > 0).sum()) if not df.empty else 0},
            {"metric": "Generated at", "value": datetime.now().isoformat(timespec="seconds")},
        ])
        summary.to_excel(writer, sheet_name="Dashboard", index=False)
        qdf = pd.DataFrame({"query": load_lines(Path("config/queries_all.txt"))})
        qdf.to_excel(writer, sheet_name="Queries", index=False)
        ws = writer.book["Leads"]
        ws.freeze_panes = "A2"
        for col in ws.columns:
            max_len = min(max(len(str(c.value or "")) for c in col) + 2, 55)
            ws.column_dimensions[col[0].column_letter].width = max_len
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


def run() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="BlueBoot agency lead finder")
    parser.add_argument("--countries", default=os.getenv("COUNTRIES", "NO"), help="Comma separated country codes: NO,SE,DK,DE,UK or ALL")
    parser.add_argument("--queries", default=os.getenv("QUERIES_FILE", ""), help="Optional custom query file. If empty, loads config/queries_<COUNTRY>.txt")
    parser.add_argument("--output", default="output")
    parser.add_argument("--max-results", type=int, default=int(os.getenv("MAX_RESULTS_PER_QUERY", "20")))
    parser.add_argument("--max-pages", type=int, default=int(os.getenv("MAX_PAGES_PER_SITE", "6")))
    parser.add_argument("--delay", type=float, default=float(os.getenv("REQUEST_DELAY_SECONDS", "1.0")))
    args = parser.parse_args()

    configs = load_country_configs()
    countries = selected_countries(args.countries, configs) or DEFAULT_COUNTRIES
    query_pairs = load_queries_for_countries(countries, args.queries or None)
    blocklist = set(load_lines(Path("config/blocklist_domains.txt")))
    candidates: list[tuple[str, str]] = []
    seen_domains = set()

    print(f"Countries: {', '.join(countries)}")
    print(f"Running {len(query_pairs)} search queries...")
    for query, query_country in query_pairs:
        urls = google_cse_search(query, args.max_results) or bing_search(query, args.max_results)
        for raw in urls:
            url = clean_search_url(raw)
            dom = domain_of(url)
            detected_country = country_for_domain(dom, countries, configs) or (query_country if query_country != "AUTO" else None)
            if allowed_domain(dom, blocklist, countries, configs) and dom not in seen_domains and detected_country:
                candidates.append((url, query, detected_country))
                seen_domains.add(dom)
        time.sleep(args.delay)

    print(f"Crawling {len(candidates)} candidate domains...")
    leads: list[Lead] = []
    for i, (url, query, country_code) in enumerate(candidates, 1):
        print(f"[{i}/{len(candidates)}] {country_code} {url}")
        lead = crawl_site(url, query, args.max_pages, args.delay, country_code, configs.get(country_code, {}))
        if lead:
            print(f"  -> {lead.priority} score={lead.reseller_score} email={'yes' if lead.emails else 'no'}")
            leads.append(lead)

    leads = dedupe_leads(leads)
    export(leads, Path(args.output))
    print(f"Done. Exported {len(leads)} leads to {args.output}/agency_leads.xlsx")

if __name__ == "__main__":
    run()
