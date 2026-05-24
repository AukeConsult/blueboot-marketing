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

import asyncio

import aiohttp
import pandas as pd
import phonenumbers
import requests
import tldextract
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from rapidfuzz import fuzz


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


def fetch(url: str, timeout=15, accept_language="en;q=0.8", browser_ua: bool = False) -> str:
    if browser_ua:
        headers = {
            "User-Agent": BING_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": accept_language if accept_language != "en;q=0.8" else "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        }
    else:
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
    _strip_tags = re.compile(r"<[^>]+>")
    for e in raw_emails:
        e = e.strip(".,;:()[]<>").lower()
        # Strip any residual HTML tags (defensive — e.g. from obfuscated mailto links)
        e = _strip_tags.sub("", e).strip()
        if not e or "@" not in e:
            continue
        if any(e.endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"]):
            continue
        # Reject package version strings like react-dom@18.3.1 — domain part is all digits/dots
        domain_part = e.split("@", 1)[-1]
        if all(seg.isdigit() for seg in domain_part.split(".")):
            continue
        # Reject if TLD is purely numeric (e.g. @1.2.3 → tld "3")
        tld = domain_part.rsplit(".", 1)[-1]
        if tld.isdigit():
            continue
        if e in contacts:
            continue
        # Search ±300 chars around the email for a job title — strip tags from result
        title = ""
        idx = combined.lower().find(e)
        if idx != -1:
            window = combined[max(0, idx - 300): idx + 300]
            m = TITLE_KEYWORDS.search(window)
            if m:
                raw_title = window[m.start(): m.start() + 120]
                # Strip HTML tags, collapse whitespace, trim to 60 chars
                title = _strip_tags.sub(" ", raw_title)
                title = re.sub(r"\s+", " ", title).strip()[:60]
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
    return Lead(
        company=company_from_domain(dom), domain=dom, website=website, source_query=source_query,
        title=title, description=desc,
        emails=", ".join(sorted(contacts.keys())),
        email_titles=", ".join(contacts.get(e, "") for e in sorted(contacts.keys())),
        phones=", ".join(sorted(phones)),
        contact_page=contact_page, linkedin=linkedin or _linkedin_hints.get(website, ""), detected_tech=", ".join(sorted(tech)),
        categories=", ".join(sorted(cats)), reseller_score=score, priority=priority(score), reasons="; ".join(reasons),
        suggested_angle=lead_angle,
        country=country_code, country_name=country_cfg.get("name", country_code),
        crawled_at=datetime.now(timezone.utc).isoformat(timespec="seconds")
    )


async def async_fetch(
    session: aiohttp.ClientSession,
    url: str,
    accept_language: str = "en;q=0.8",
) -> str:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": accept_language}
    async with session.get(url, headers=headers, allow_redirects=True) as resp:
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        if "text" not in ct and "html" not in ct:
            return ""
        text = await resp.text(errors="replace")
        return text[:2_000_000]


async def async_crawl_site(
    session: aiohttp.ClientSession,
    url: str,
    source_query: str,
    max_pages: int,
    delay: float,
    country_code: str,
    country_cfg: dict,
) -> Lead | None:
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
            html = await async_fetch(session, page, country_cfg.get("accept_language", "en;q=0.8"))
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
        for lnk in links:
            low = lnk.lower().split("#")[0]
            if domain_of(low) == dom and any(
                x in low for x in country_cfg.get("contact_words", ["contact", "about", "services", "case"])
            ):
                if low not in seen and low not in queue:
                    queue.append(low)
        await asyncio.sleep(delay)

    if not all_text and not all_html:
        return None
    cats, reasons, score = categorize(all_text, all_html, country_cfg)
    if score < 20 and fuzz.partial_ratio("digitalbyrå webdesign wordpress seo", all_text[:5000].lower()) < 35:
        return None
    lead_angle = angle(cats, tech)
    return Lead(
        company=company_from_domain(dom), domain=dom, website=website, source_query=source_query,
        title=title, description=desc,
        emails=", ".join(sorted(contacts.keys())),
        email_titles=", ".join(contacts.get(e, "") for e in sorted(contacts.keys())),
        phones=", ".join(sorted(phones)),
        contact_page=contact_page,
        linkedin=linkedin or _linkedin_hints.get(website, ""),
        detected_tech=", ".join(sorted(tech)),
        categories=", ".join(sorted(cats)),
        reseller_score=score, priority=priority(score), reasons="; ".join(reasons),
        suggested_angle=lead_angle,
        country=country_code, country_name=country_cfg.get("name", country_code),
        crawled_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
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


def load_existing_leads(output_path: Path) -> list[Lead]:
    """Load all leads from a previous run's CSV so we never lose them on re-run."""
    csv_path = output_path / "agency_leads.csv"
    if not csv_path.exists():
        return []
    try:
        df = pd.read_csv(csv_path, dtype=str).fillna("")
        # Drop the lead_id column that export() prepends — it's not a Lead field
        df = df.drop(columns=["lead_id"], errors="ignore")
        fields = {f.name for f in Lead.__dataclass_fields__.values()}
        df = df[[c for c in df.columns if c in fields]]
        leads = []
        for row in df.to_dict(orient="records"):
            # Coerce numeric fields back from str
            for int_field in ("reseller_score",):
                if int_field in row:
                    try:
                        row[int_field] = int(float(row[int_field]))
                    except (ValueError, TypeError):
                        row[int_field] = 0
            leads.append(Lead(**{k: v for k, v in row.items() if k in fields}))
        print(f"Loaded {len(leads)} existing leads from {csv_path}")
        return leads
    except Exception as e:
        print(f"Warning: could not read existing CSV ({e}) — starting fresh")
        return []


def load_existing_domains(output_path: Path) -> set[str]:
    """Return domains already crawled (derived from load_existing_leads)."""
    leads = load_existing_leads(output_path)
    domains = {l.domain.strip().lower() for l in leads if l.domain}
    if domains:
        print(f"  ({len(domains)} already-crawled domains loaded)")
    return domains


async def _run_batch(
    batch: list[tuple[str, str]],
    args,
    code: str,
    configs: dict,
    all_leads: list[Lead],
    export_path: Path,
    country_leads: dict,
) -> int:
    """Async: create one Task per site, run all concurrently, save each lead as it arrives."""
    n = len(batch)
    print(f"\n  >>> Crawling {n} site{'s' if n > 1 else ''} in parallel <<<")
    timeout = aiohttp.ClientTimeout(total=60, connect=10)
    connector = aiohttp.TCPConnector(limit=n, ssl=False)
    new_count = 0

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        tasks = [
            asyncio.create_task(
                async_crawl_site(session, url, query, args.max_pages, args.delay, code, configs.get(code, {}))
            )
            for url, query in batch
        ]
        for coro in asyncio.as_completed(tasks):
            try:
                lead = await coro
            except Exception as exc:
                print(f"    [crawl error]: {exc}")
                continue
            if lead:
                has_email = "yes" if lead.emails else "no"
                print(f"    -> {lead.priority} score={lead.reseller_score} email={has_email}  {lead.website}")
                all_leads.append(lead)
                country_leads[code] = country_leads.get(code, 0) + 1
                new_count += 1
                export(dedupe_leads(all_leads), export_path)  # sync write — fine in single-threaded event loop
    return new_count


def _crawl_batch(
    batch: list[tuple[str, str]],
    args,
    code: str,
    configs: dict,
    all_leads: list[Lead],
    export_path: Path,
    country_leads: dict,
) -> int:
    """Sync entry point — runs the async batch in a fresh event loop."""
    if not batch:
        return 0
    return asyncio.run(_run_batch(batch, args, code, configs, all_leads, export_path, country_leads))


def run(args=None) -> None:
    configs = load_country_configs()
    countries = selected_countries(args.countries, configs) or DEFAULT_COUNTRIES
    query_pairs = load_queries_for_countries(countries, args.queries or None)
    blocklist = set(load_lines(Path("config/blocklist_domains.txt")))

    # --- Load previous run so we never lose existing data ---
    all_leads: list[Lead] = load_existing_leads(Path(args.output))
    seen_domains: set[str] = {l.domain.strip().lower() for l in all_leads if l.domain}

    # --- Build per-country query queues ---
    from collections import defaultdict
    queues: dict[str, list[str]] = defaultdict(list)
    for q, c in query_pairs:
        queues[c].append(q)

    # Per-country state
    country_leads: dict[str, int] = {c: 0 for c in countries}   # leads found this run
    country_streak: dict[str, int] = {c: 0 for c in countries}  # consecutive queries with 0 new candidates
    country_done: set[str] = set()

    total_queries_run = 0
    batch_size: int = args.workers   # collect this many sites then crawl them all at once

    # Per-country pending queue (accumulates across queries until we have a full batch)
    pending: dict[str, list[tuple[str, str]]] = {c: [] for c in countries}

    print(f"Countries: {', '.join(countries)}")
    print(f"Batch size (parallel crawlers): {batch_size}")
    if args.max_country:
        print(f"Target: {args.max_country} leads per country  |  Give up after {args.give_up_after} empty queries in a row")
    else:
        print(f"No per-country cap  |  Give up after {args.give_up_after} empty queries in a row")

    def _flush_pending(code: str, label: str = "") -> None:
        if not pending[code]:
            return
        batch = pending[code]
        pending[code] = []
        if label:
            print(f"  [{code}] {label}")
        _crawl_batch(batch, args, code, configs, all_leads, Path(args.output), country_leads)

    # Round-robin across countries until all are done
    while len(country_done) < len(countries):
        made_progress = False
        for code in countries:
            if code in country_done:
                continue

            # Check cap — flush remainder first
            if args.max_country and country_leads[code] >= args.max_country:
                print(f"\n[{code}] Target of {args.max_country} leads reached.")
                country_done.add(code)
                continue

            # Check query queue — flush remainder when exhausted
            if not queues[code]:
                _flush_pending(code, f"Final batch of {len(pending[code])} sites")
                print(f"\n[{code}] No more queries to run — giving up.")
                country_done.add(code)
                continue

            query = queues[code].pop(0)
            total_queries_run += 1
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

            # Filter candidates and accumulate into pending
            added = 0
            for raw in urls:
                url = clean_search_url(raw)
                dom = domain_of(url)
                detected_country = country_for_domain(dom, countries, configs) or code
                if is_product_or_content_url(url):
                    parsed = urlparse(url)
                    url = f"{parsed.scheme}://{parsed.netloc}/"
                if allowed_domain(dom, blocklist, countries, configs) and dom not in seen_domains and detected_country == code:
                    if args.max_country and len(pending[code]) + country_leads[code] >= args.max_country:
                        break
                    pending[code].append((url, query))
                    seen_domains.add(dom)
                    added += 1

            print(f"  -> {added} new candidates  (pending={len(pending[code])})")

            # When we have a full batch, crawl them all in parallel
            if len(pending[code]) >= batch_size:
                batch = pending[code][:batch_size]
                pending[code] = pending[code][batch_size:]
                _crawl_batch(batch, args, code, configs, all_leads, Path(args.output), country_leads)

            # Update streak: reset if we found new URLs, else increment
            if added:
                country_streak[code] = 0
            else:
                country_streak[code] += 1
                print(f"  [{code}] Nothing new — streak {country_streak[code]}/{args.give_up_after}")
                if country_streak[code] >= args.give_up_after:
                    _flush_pending(code, f"Final batch of {len(pending[code])} sites")
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


# ---------------------------------------------------------------------------
# Catalog scraping — per-directory link extractors
# ---------------------------------------------------------------------------

CATALOG_CONFIG_PATH = Path("config/catalogs.json")


def load_catalogs(path: Path = CATALOG_CONFIG_PATH) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def catalog_links_generic(url: str, blocklist: set[str]) -> list[str]:
    """Extract all outbound homepage-like hrefs from a listing page."""
    try:
        html = fetch(url, timeout=20, browser_ua=True)
    except Exception as e:
        print(f"    [catalog] fetch error: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    found: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith("http"):
            href = urljoin(url, href)
        parsed = urlparse(href)
        # Only external links (different domain from the catalog page)
        catalog_dom = domain_of(url)
        link_dom = domain_of(href)
        if link_dom and link_dom != catalog_dom and not is_blocked(link_dom, blocklist):
            # Normalise to homepage
            home = f"{parsed.scheme}://{parsed.netloc}/"
            if home not in seen:
                seen.add(home)
                found.append(home)
    return found


def catalog_links_clutch(url: str, blocklist: set[str]) -> list[str]:
    """Clutch profile pages link to agency websites via a rel=nofollow external link."""
    try:
        html = fetch(url, timeout=20, accept_language="en-US,en;q=0.9", browser_ua=True)
    except Exception as e:
        print(f"    [catalog/clutch] fetch error: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    found: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith("http"):
            continue
        link_dom = domain_of(href)
        catalog_dom = domain_of(url)
        if link_dom and link_dom != catalog_dom and not is_blocked(link_dom, blocklist):
            parsed = urlparse(href)
            home = f"{parsed.scheme}://{parsed.netloc}/"
            if home not in seen:
                seen.add(home)
                found.append(home)
    return found


# Module-level cache: website home URL → LinkedIn URL, populated by Sortlist extractor
_linkedin_hints: dict[str, str] = {}


def _sortlist_urls_from_json(obj, blocklist: set[str]) -> list[str]:
    """Recursively walk a Sortlist Next.js / Apollo cache JSON, collect agency website URLs.

    Also populates the module-level _linkedin_hints dict when website + linkedin keys
    appear together in the same agency object.
    """
    # All keys lowercased so k.lower() comparison works correctly
    URL_KEYS = {
        "website", "websiteurl", "websiteuri", "web", "siteurl",
        "externalurl", "external_url", "homepage", "homepagelinks",
        "link", "links",
    }
    # 'domain' key holds bare domain names — we'll construct https:// URLs from them
    DOMAIN_KEYS = {"domain"}
    # Social keys we look for alongside 'website' in the same dict
    SOCIAL_KEYS = {"linkedin", "facebook", "instagram", "twitter", "x"}

    found: list[str] = []
    seen: set[str] = set()

    def _add(home: str) -> None:
        if home not in seen:
            seen.add(home)
            found.append(home)

    def _collect_url(v: str) -> None:
        v = v.strip()
        if not v.startswith("http"):
            return
        dom = domain_of(v)
        if not dom or "sortlist" in dom or is_blocked(dom, blocklist):
            return
        parsed = urlparse(v)
        _add(f"{parsed.scheme}://{parsed.netloc}/")

    def _collect_domain(v: str) -> None:
        """Handle bare domain values like 'myagency.com' or 'www.myagency.com'."""
        v = v.strip().rstrip("/")
        if not v or " " in v or "." not in v:
            return
        if "/" in v or "sortlist" in v:
            return
        dom = domain_of("https://" + v)
        if not dom or is_blocked(dom, blocklist):
            return
        _add(f"https://{v}/")

    def _walk(node) -> None:
        if isinstance(node, dict):
            # Build a lowercase-key → value map for this dict
            lc = {k.lower(): v for k, v in node.items()}

            # If this dict has a 'website' key, try to capture LinkedIn from same dict
            if "website" in lc and isinstance(lc["website"], str):
                raw = lc["website"].strip()
                if raw.startswith("http"):
                    parsed = urlparse(raw)
                    home = f"{parsed.scheme}://{parsed.netloc}/"
                    dom = domain_of(raw)
                    if dom and "sortlist" not in dom and not is_blocked(dom, blocklist):
                        # Check for linkedin directly in this dict or one level down
                        li_url = ""
                        if "linkedin" in lc and isinstance(lc["linkedin"], str) and "linkedin.com" in lc["linkedin"]:
                            li_url = lc["linkedin"]
                        elif "socialprofiles" in lc and isinstance(lc["socialprofiles"], dict):
                            sp = {k.lower(): v for k, v in lc["socialprofiles"].items()}
                            if "linkedin" in sp and isinstance(sp["linkedin"], str):
                                li_url = sp["linkedin"]
                        if li_url and home not in _linkedin_hints:
                            _linkedin_hints[home] = li_url

            for k, v in node.items():
                kl = k.lower()
                if kl in URL_KEYS:
                    if isinstance(v, str):
                        _collect_url(v)
                    else:
                        _walk(v)
                elif kl in DOMAIN_KEYS:
                    if isinstance(v, str):
                        _collect_domain(v)
                else:
                    _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(obj)
    return found


def catalog_links_sortlist(url: str, blocklist: set[str]) -> list[str] | None:
    """Sortlist is a Next.js SPA — agency data lives in <script id='__NEXT_DATA__'> JSON.
    Uses minimal headers (no Sec-Fetch-*) to avoid Sortlist's bot detection.
    Parse that first; fall back to <a> scanning if not found."""
    # Minimal headers only — Sec-Fetch-* headers trigger Sortlist bot detection
    _ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    try:
        r = requests.get(url, headers={"User-Agent": _ua, "Accept-Language": "en;q=0.8"}, timeout=20, allow_redirects=True)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"    [catalog/sortlist] fetch error: {e}")
        return None

    soup = BeautifulSoup(html, "html.parser")

    # Primary: parse Next.js embedded JSON
    script = soup.find("script", id="__NEXT_DATA__")
    if script and script.string:
        try:
            data = json.loads(script.string)
        except json.JSONDecodeError as e:
            print(f"    [catalog/sortlist] __NEXT_DATA__ JSON parse error: {e}")
            data = None

        if data is not None:
            try:
                found = _sortlist_urls_from_json(data, blocklist)
            except Exception as e:
                import traceback
                print(f"    [catalog/sortlist] walker error: {e}")
                traceback.print_exc()
                found = []
            if found:
                return found
            print(f"    [catalog/sortlist] __NEXT_DATA__ parsed OK but 0 URLs extracted")

    # Fallback: scan <a> links (works if Sortlist ever serves static HTML)
    catalog_dom = domain_of(url)
    found, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith("http"):
            href = urljoin(url, href)
        dom = domain_of(href)
        if dom and dom != catalog_dom and not is_blocked(dom, blocklist):
            home = f"{urlparse(href).scheme}://{urlparse(href).netloc}/"
            if home not in seen:
                seen.add(home)
                found.append(home)
    return found  # [] means client-side rendered with no data at all


def catalog_links_designrush(url: str, blocklist: set[str]) -> list[str]:
    """DesignRush agency listing page — extract agency website URLs.
    Uses minimal headers (no Sec-Fetch-*) to avoid WAF, tries __NEXT_DATA__ first."""
    _ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    try:
        r = requests.get(url, headers={"User-Agent": _ua, "Accept-Language": "en;q=0.8"}, timeout=20, allow_redirects=True)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"    [catalog/designrush] fetch error: {e}")
        return None

    print(f"    [catalog/designrush] fetched {len(html):,} chars", end="")

    found: list[str] = []
    seen: set[str] = set()
    catalog_dom = "designrush.com"

    def _collect(raw: str) -> None:
        raw = raw.strip()
        if not raw.startswith("http"):
            return
        dom = domain_of(raw)
        if dom and dom != catalog_dom and not is_blocked(dom, blocklist):
            parsed = urlparse(raw)
            home = f"{parsed.scheme}://{parsed.netloc}/"
            if home not in seen:
                seen.add(home)
                found.append(home)

    # --- Try __NEXT_DATA__ first ---
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if script and script.string:
        print(" | __NEXT_DATA__: FOUND", end="")
        try:
            data = json.loads(script.string)
            # Walk entire JSON looking for website/url fields pointing off-site
            WEBSITE_KEYS = {"website", "websiteurl", "websiteuri", "web", "siteurl",
                            "externalurl", "external_url", "homepage", "url", "profile_url",
                            "company_url", "companyurl"}
            def _walk(node) -> None:
                if isinstance(node, dict):
                    lc = {k.lower(): v for k, v in node.items()}
                    for key in WEBSITE_KEYS:
                        if key in lc and isinstance(lc[key], str):
                            _collect(lc[key])
                    for v in node.values():
                        _walk(v)
                elif isinstance(node, list):
                    for item in node:
                        _walk(item)
            _walk(data)
        except Exception as e:
            print(f" | JSON parse error: {e}", end="")

    # --- Fallback: scan <a> tags for external links ---
    if not found:
        print(" | falling back to <a> scan", end="")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href.startswith("http"):
                href = urljoin(url, href)
            _collect(href)

    print(f" → {len(found)} URLs")
    return found


def catalog_links_goodfirms(url: str, blocklist: set[str]) -> list[str] | None:
    """GoodFirms listing page → follow internal /company/ profile links → extract agency websites.
    GoodFirms listing pages don't link directly to external sites; only profile pages do."""
    _ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    _headers = {"User-Agent": _ua, "Accept-Language": "en;q=0.8"}
    try:
        r = requests.get(url, headers=_headers, timeout=20, allow_redirects=True)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"    [catalog/goodfirms] fetch error: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")

    # Collect unique /company/ profile paths from the listing page
    profile_urls: list[str] = []
    seen_profiles: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "/company/" in href:
            if href.startswith("/"):
                href = "https://www.goodfirms.co" + href
            href = href.split("?")[0].split("#")[0]
            if href not in seen_profiles:
                seen_profiles.add(href)
                profile_urls.append(href)

    if not profile_urls:
        return []  # listing page has no company links → catalog exhausted

    # Fetch each profile page and grab the first external link (the agency website)
    found: list[str] = []
    seen_doms: set[str] = set()
    catalog_dom = "goodfirms.co"
    for profile_url in profile_urls:
        try:
            pr = requests.get(profile_url, headers=_headers, timeout=12, allow_redirects=True)
            pr.raise_for_status()
            phtml = pr.text
        except Exception:
            continue
        psoup = BeautifulSoup(phtml, "html.parser")
        for a in psoup.find_all("a", href=True):
            href = a["href"].strip()
            if not href.startswith("http"):
                continue
            dom = domain_of(href)
            if dom and dom != catalog_dom and not is_blocked(dom, blocklist) and dom not in seen_doms:
                parsed = urlparse(href)
                home = f"{parsed.scheme}://{parsed.netloc}/"
                seen_doms.add(dom)
                found.append(home)
                break  # one website per agency profile
        time.sleep(0.5)

    return found


def catalog_links_gulesider(url: str, blocklist: set[str]) -> list[str]:
    """Gule Sider / De Gule Sider -- extract business website links."""
    try:
        html = fetch(url, timeout=20, browser_ua=True)
    except Exception as e:
        print(f"    [catalog/gulesider] fetch error: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    found: list[str] = []
    seen: set[str] = set()
    catalog_dom = domain_of(url)
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith("http"):
            href = urljoin(url, href)
        link_dom = domain_of(href)
        if link_dom and link_dom != catalog_dom and not is_blocked(link_dom, blocklist):
            parsed = urlparse(href)
            home = f"{parsed.scheme}://{parsed.netloc}/"
            if home not in seen:
                seen.add(home)
                found.append(home)
    return found


def catalog_links_proff(url: str, blocklist: set[str]) -> list[str]:
    """Proff.no business registry -- extract company website links."""
    try:
        html = fetch(url, timeout=20, browser_ua=True)
    except Exception as e:
        print(f"    [catalog/proff] fetch error: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    found: list[str] = []
    seen: set[str] = set()
    catalog_dom = domain_of(url)
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith("http"):
            href = urljoin(url, href)
        link_dom = domain_of(href)
        if link_dom and link_dom != catalog_dom and not is_blocked(link_dom, blocklist):
            parsed = urlparse(href)
            home = f"{parsed.scheme}://{parsed.netloc}/"
            if home not in seen:
                seen.add(home)
                found.append(home)
    return found


def catalog_links_yelp(url: str, blocklist: set[str]) -> list[str]:
    """Yelp business listings -- extract external website links."""
    return catalog_links_generic(url, blocklist)


def catalog_links_pagesjaunes(url: str, blocklist: set[str]) -> list[str]:
    """Pages Jaunes (France) -- extract business website links."""
    return catalog_links_generic(url, blocklist)


def catalog_links_paginasamarillas(url: str, blocklist: set[str]) -> list[str]:
    """Paginas Amarillas (Spain) -- extract business website links."""
    return catalog_links_generic(url, blocklist)


CATALOG_EXTRACTORS = {
    "clutch":             catalog_links_clutch,
    "sortlist":           catalog_links_sortlist,
    "designrush":         catalog_links_designrush,
    "goodfirms":          catalog_links_goodfirms,
    "gulesider":          catalog_links_gulesider,
    "proff":              catalog_links_proff,
    "yelp":               catalog_links_yelp,
    "pagesjaunes":        catalog_links_pagesjaunes,
    "paginasamarillas":   catalog_links_paginasamarillas,
    "generic":            catalog_links_generic,
}


def scrape_catalog_page(entry: dict, page: int, blocklist: set[str]) -> list[str] | None:
    """Fetch one page of a catalog and return outbound agency URLs.
    Returns None on fetch error (caller should skip page and continue).
    Returns [] when page genuinely has no outbound links (catalog exhausted).
    """
    offset = (page - 1) * 10
    url = entry["url"].format(page=page, offset=offset)
    extractor = CATALOG_EXTRACTORS.get(entry.get("type", "generic"), catalog_links_generic)
    return extractor(url, blocklist)


def catalog_run(args) -> None:
    """Mode: read from directory catalogs, crawl extracted agency sites, export leads."""
    configs = load_country_configs()
    countries = selected_countries(args.countries, configs) or DEFAULT_COUNTRIES
    blocklist = set(load_lines(Path("config/blocklist_domains.txt")))

    # Load catalogs for selected countries
    all_catalogs = load_catalogs()
    # Filter to selected countries
    catalogs: dict[str, list[dict]] = {c: all_catalogs[c] for c in countries if c in all_catalogs}

    if not catalogs:
        print(f"No catalog entries found for: {', '.join(countries)}")
        return

    # Load previous run so we never lose existing data
    all_leads: list[Lead] = load_existing_leads(Path(args.output))
    seen_domains: set[str] = {l.domain.strip().lower() for l in all_leads if l.domain}

    country_leads: dict[str, int] = {}
    batch_size: int = args.workers

    max_pages = getattr(args, "max_catalog_pages", None)

    print(f"Countries: {', '.join(countries)}")
    print(f"Batch size (parallel crawlers): {batch_size}")

    for code, sources in catalogs.items():
        print(f"\n{'='*60}")
        print(f"[{code}] {len(sources)} catalog source(s)")
        pending: list[tuple[str, str]] = []

        for entry in sources:
            name = entry.get("name", entry.get("url", "?"))
            total_pages = entry.get("pages", 1)
            if max_pages:
                total_pages = min(total_pages, max_pages)
            print(f"\n  Source: {name} (up to {total_pages} pages)")

            for page in range(1, total_pages + 1):
                print(f"  Page {page}/{total_pages}", end=" ... ", flush=True)
                links = scrape_catalog_page(entry, page, blocklist)

                if links is None:
                    print("fetch error — skipping page, continuing...")
                    continue

                if not links:
                    print("0 links found — catalog exhausted, stopping this source.")
                    break

                # Filter out already-seen domains
                new_links = []
                for url in links:
                    dom = domain_of(url)
                    if dom and dom not in seen_domains:
                        seen_domains.add(dom)
                        new_links.append((url, name))

                print(f"{len(new_links)} new candidates (of {len(links)} found)")
                pending.extend(new_links)

                # Dispatch a full batch whenever we've accumulated enough
                while len(pending) >= batch_size:
                    batch = pending[:batch_size]
                    pending = pending[batch_size:]
                    _crawl_batch(batch, args, code, configs, all_leads, Path(args.output), country_leads)

                time.sleep(args.delay)

        # Flush remaining sites for this country
        if pending:
            print(f"\n  [{code}] Flushing final batch of {len(pending)} sites")
            _crawl_batch(pending, args, code, configs, all_leads, Path(args.output), country_leads)
            pending = []

        print(f"\n[{code}] Done \u2014 {country_leads.get(code, 0)} new leads from catalogs")

    print(f"\n{'='*60}")
    print(f"Catalog run complete.")
    all_leads = dedupe_leads(all_leads)
    export(all_leads, Path(args.output))
    print(f"Exported {len(all_leads)} leads to {args.output}/agency_leads.xlsx")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="BlueBoot Lead Agent \u2014 find & score web-design agencies",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--mode", choices=["search", "catalog"], default="search",
        help="search = Bing/Google keyword search; catalog = scrape directory listings",
    )
    parser.add_argument(
        "--countries", default=None,
        help="Comma-separated ISO codes to process, e.g. NO,SE,DK. Default: all configured.",
    )
    parser.add_argument(
        "--queries", default=None,
        help="Comma-separated search queries (overrides per-country query files).",
    )
    parser.add_argument(
        "--output", default="output",
        help="Output directory for the Excel file.",
    )
    parser.add_argument(
        "--max-results", type=int, default=int(os.getenv("MAX_RESULTS", "10")),
        help="Max search results per query.",
    )
    parser.add_argument(
        "--max-pages", type=int, default=int(os.getenv("MAX_PAGES", "3")),
        help="Max pages to crawl per agency website.",
    )
    parser.add_argument(
        "--max-country", type=int, default=int(os.getenv("MAX_COUNTRY", "0")) or None,
        help="Stop a country after this many leads (0 = unlimited).",
    )
    parser.add_argument(
        "--give-up-after", type=int, default=int(os.getenv("GIVE_UP_AFTER", "10")),
        help="Give up a country after this many consecutive empty queries.",
    )
    parser.add_argument(
        "--delay", type=float, default=float(os.getenv("CRAWL_DELAY", "1.0")),
        help="Seconds to wait between page fetches within one site.",
    )
    parser.add_argument(
        "--workers", type=int, default=int(os.getenv("CRAWL_WORKERS", "20")),
        help="Parallel site-crawl workers / batch size (default 20).",
    )
    parser.add_argument(
        "--max-catalog-pages", type=int, default=None,
        help="Limit pages per catalog source (for testing).",
    )
    return parser


def main() -> None:
    load_dotenv()
    parser = _build_parser()
    args = parser.parse_args()
    if args.mode == "catalog":
        catalog_run(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
atalog source (for testing).",
    )
    return parser


def main() -> None:
    load_dotenv()
    parser = _build_parser()
    args = parser.parse_args()
    if args.mode == "catalog":
        catalog_run(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
