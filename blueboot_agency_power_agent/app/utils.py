"""Shared utilities — no internal project imports."""
from __future__ import annotations

import json
import os
import re
import time
from fnmatch import fnmatch
from pathlib import Path
from urllib.parse import urljoin, urlparse

import phonenumbers
import requests
import tldextract
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENT      = "BlueBootLeadAgent/1.1 (+https://blueboot.ai)"
BROWSER_UA      = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
EMAIL_RE        = re.compile(r"[a-zA-Z0-9_.+\-]+@[a-zA-Z0-9\-]+(?:\.[a-zA-Z0-9\-]+)+")
GENERIC_PHONE_RE = re.compile(r"(?:(?:\+|00)\d{1,3}\s*)?(?:\d[\s().-]?){7,15}")
DEFAULT_COUNTRIES      = ["NO"]
COUNTRY_CONFIG_PATH    = Path("config/countries.json")

PRODUCT_PAGE_PATTERNS = [
    "/c/", "/category/", "/katalog/", "/kategori/",
    "/product/", "/produkt/", "/products/", "/produkter/",
    "/shop/", "/store/", "/butikk/",
    "/p/", "/item/", "/items/",
    "/blogg/", "/blog/", "/news/", "/nyheter/",
    "/article/", "/artikkel/",
]

# Two or three consecutive capitalised words — person name candidate
# Supports most Western-European accented characters
_NAME_RE = re.compile(
    r'\b([A-ZÁÉÍÓÚÀÈÌÒÙÄËÏÖÜÅÆØČŠŽĆĐŃŁŻŹ][a-záéíóúàèìòùäëïöüåæøčšžćđńłżź]{1,25}'
    r'(?:[ \-][A-ZÁÉÍÓÚÀÈÌÒÙÄËÏÖÜÅÆØČŠŽĆĐŃŁŻŹ][a-záéíóúàèìòùäëïöüåæøčšžćđńłżź]{1,25}){1,2})\b'
)

# Generic words that pattern-match as names but aren't people
_NAME_BLACKLIST = re.compile(
    r'^(?:Contact|About|Our|Get|Send|Read|Learn|View|Click|More|Info|Home|'
    r'Page|Site|Blog|News|Team|Staff|Work|Project|Service|Solution|Design|'
    r'Media|Content|Online|Mobile|Search|Data|Tech|Powered|Built|Made|'
    r'Created|Copyright|All|Rights|Reserved|Web|Digital|Social|Email|Phone|'
    r'Address|Welcome|Hello|Thanks|Please|Follow|Subscribe|Login|Register|'
    r'Privacy|Policy|Terms|Cookie|Norway|Sverige|Danmark|Finland|Germany|'
    r'France|Spain|Italy|Poland|Hungary|Estonia|Latvia|Lithuania|India|Tunisia|'
    r'Netherlands|Belgium|Austria|Ireland|United|Kingdom)$',
    re.IGNORECASE,
)

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

TECH_SIGNATURES = {
    "WordPress":      ["wp-content", "wp-includes", "wordpress"],
    "WooCommerce":    ["woocommerce", "wc-blocks", "wp-content/plugins/woocommerce"],
    "Elementor":      ["elementor-frontend", "elementor/assets"],
    "Divi":           ["et-pb-", "divi/js", "extra/css"],
    "Episerver":      ["episerver", "epi-", "/EPiServer/", "episerver.js"],
    "Optimizely CMS": ["optimizely", "optimizelycms", "optly"],
    "Umbraco":        ["umbraco", "/umbraco/", "umbraco.js"],
    "Sitecore":       ["sitecore", "/-/media/", "sitecore/shell"],
    "Kentico":        ["kentico", "cmsdesk", "/CMSPages/"],
    "TYPO3":          ["typo3", "typo3conf", "typo3/sysext"],
    "DotNetNuke":     ["dnn", "dotnetnuke", "/desktopmodules/"],
    "Shopify":        ["cdn.shopify.com", "shopify"],
    "Magento":        ["magento", "mage/", "Magento_"],
    "PrestaShop":     ["prestashop", "/modules/prestashop", "presta-"],
    "Shopware":       ["shopware", "sw-plugin"],
    "BigCommerce":    ["bigcommerce", "bc-sf-filter"],
    "OpenCart":       ["opencart", "catalog/view/theme"],
    "Contentful":     ["contentful", "ctfassets.net"],
    "Sanity":         ["sanity.io", "cdn.sanity.io"],
    "Storyblok":      ["storyblok", "a.storyblok.com"],
    "Prismic":        ["prismic.io", "cdn.prismic.io"],
    "Craft CMS":      ["craftcms", "craft-cms"],
    "Ghost":          ["ghost.io", "/ghost/", "ghost-theme"],
    "Webflow":        ["webflow", "assets.website-files.com"],
    "Squarespace":    ["squarespace"],
    "Wix":            ["wixstatic", "wix.com"],
    "Framer":         ["framer.com", "framerusercontent.com"],
    "HubSpot":        ["hs-scripts", "hubspot"],
    "Salesforce":     ["salesforce", "force.com", "sfdcstatic"],
    "Marketo":        ["marketo", "munchkin.js"],
    "ActiveCampaign": ["activecampaign", "trackcmp.net"],
}

# Module-level cache: website home URL → LinkedIn URL (populated by catalog_scrapers)
linkedin_hints: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Content negative keywords — loaded once from config/blocklist_domains.txt
# ---------------------------------------------------------------------------

def _load_content_negative_keywords() -> list[str]:
    """Read the CONTENT NEGATIVE KEYWORDS section from blocklist_domains.txt."""
    bl = Path(__file__).parent.parent / "config" / "blocklist_domains.txt"
    if not bl.exists():
        return []
    in_section = False
    kws: list[str] = []
    # A real section header looks like:  # === NAME === or # === NAME ====
    # Pure divider lines (# ===...===) have no words between the === markers.
    _section_re = re.compile(r"^#\s*===\s*\w")
    for raw in bl.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if "CONTENT NEGATIVE KEYWORDS" in line:
            in_section = True
            continue
        if in_section:
            # Stop only on a real named section header, not a pure ===== divider
            if _section_re.match(line) and "CONTENT NEGATIVE KEYWORDS" not in line:
                break
            if line and not line.startswith("#"):
                kws.append(line.lower())
    return kws

_CONTENT_NEG_KWS: list[str] = _load_content_negative_keywords()


# ---------------------------------------------------------------------------
# URL / domain helpers
# ---------------------------------------------------------------------------

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


def is_product_or_content_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(pat in path for pat in PRODUCT_PAGE_PATTERNS)


def is_blocked(domain: str, blocklist: set[str]) -> bool:
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

# ---------------------------------------------------------------------------
# Config / file helpers
# ---------------------------------------------------------------------------

def load_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [x.strip() for x in path.read_text(encoding="utf-8").splitlines()
            if x.strip() and not x.strip().startswith("#")]


def load_country_configs(path: Path = COUNTRY_CONFIG_PATH) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def selected_countries(value: str | None, configs: dict) -> list[str]:
    if not value:
        return []
    if value.upper() == "ALL":
        return list(configs.keys())
    result = [x.strip().upper() for x in value.split(",") if x.strip()]
    return [x for x in result if x in configs]

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def fetch(url: str, timeout: int = 15, accept_language: str = "en;q=0.8",
          browser_ua: bool = False) -> str:
    if browser_ua:
        headers = {
            "User-Agent": BROWSER_UA,
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
    ct = r.headers.get("content-type", "")
    if "text" not in ct and "html" not in ct:
        return ""
    return r.text[:2_000_000]

# ---------------------------------------------------------------------------
# HTML extraction
# ---------------------------------------------------------------------------

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


def extract_contacts(html: str, text: str) -> dict[str, str]:
    """Return {email: title} — title is best-effort from surrounding text."""
    combined = html + " " + text
    raw_emails = EMAIL_RE.findall(combined)
    contacts: dict[str, str] = {}
    _strip_tags = re.compile(r"<[^>]+>")
    for e in raw_emails:
        e = e.strip(".,;:()[]<>").lower()
        e = _strip_tags.sub("", e).strip()
        if not e or "@" not in e:
            continue
        if any(e.endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"]):
            continue
        domain_part = e.split("@", 1)[-1]
        if all(seg.isdigit() for seg in domain_part.split(".")):
            continue
        tld = domain_part.rsplit(".", 1)[-1]
        if tld.isdigit():
            continue
        # Reject hash/UUID local parts — automated addresses like
        # bfb679c754744c58a7374ee6e25cfc13@sentry.wixpress.com
        local_part = e.split("@", 1)[0]
        if len(local_part) >= 16 and re.fullmatch(r"[0-9a-f\-]+", local_part):
            continue
        if e in contacts:
            continue
        title = ""
        idx = combined.lower().find(e)
        if idx != -1:
            window = combined[max(0, idx - 300): idx + 300]
            m = TITLE_KEYWORDS.search(window)
            if m:
                raw_title = window[m.start(): m.start() + 120]
                title = _strip_tags.sub(" ", raw_title)
                title = re.sub(r"\s+", " ", title).strip()[:60]
        contacts[e] = title
    return contacts


def extract_phones(text: str, country: str = "NO") -> set[str]:
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


def _parse_phone(raw: str, country: str) -> str | None:
    """Parse and format a single phone string; return None if invalid."""
    clean = re.sub(r"[^+0-9]", "", raw)
    try:
        num = phonenumbers.parse(clean, country)
        if phonenumbers.is_valid_number(num):
            return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    except Exception:
        pass
    return None


def pair_phones_to_contacts(
    contacts: dict[str, str],
    combined: str,
    country: str = "NO",
) -> dict[str, str]:
    """Return {email: phone_or_empty}.

    Searches the line containing the email plus the two adjacent lines for a
    valid phone number.  If found it is attached to that contact; if not the
    contact gets an empty string so the caller can omit the field rather than
    writing a page-level phone that belongs to nobody.
    """
    lines = combined.splitlines()
    result: dict[str, str] = {}
    for email in contacts:
        email_l = email.lower()
        phone = ""
        for i, line in enumerate(lines):
            if email_l in line.lower():
                # 1st pass: same line (avoids bleed from adjacent contacts)
                for m in GENERIC_PHONE_RE.findall(line):
                    parsed = _parse_phone(m, country)
                    if parsed:
                        phone = parsed
                        break
                # 2nd pass: next line only, but skip if it contains another email
                if not phone and i + 1 < len(lines):
                    next_line = lines[i + 1]
                    if not EMAIL_RE.search(next_line):
                        for m in GENERIC_PHONE_RE.findall(next_line):
                            parsed = _parse_phone(m, country)
                            if parsed:
                                phone = parsed
                                break
                break  # stop at first occurrence of the email
        result[email] = phone
    return result


def pair_names_to_contacts(
    contacts: dict[str, str],
    combined: str,
    html: str = "",
) -> dict[str, str]:
    """Return {email: person_name_or_empty}.

    Three strategies in descending reliability:
    1. <a href="mailto:EMAIL">Name Text</a> anchor text — most reliable.
    2. _NAME_RE match on the same line as the email (email token stripped first).
    3. _NAME_RE match on up to 3 lines *above* the email line (stop if another
       email address appears on an above line, to avoid cross-contact bleed).
    """
    result: dict[str, str] = {}

    # Strategy 1: parse mailto anchors from raw HTML
    mailto_names: dict[str, str] = {}
    if html:
        _anchor_re = re.compile(
            r'<a[^>]+href=["\']mailto:([^"\'>\s]+)["\'][^>]*>([^<]{1,80})</a>',
            re.IGNORECASE,
        )
        for m in _anchor_re.finditer(html):
            addr = m.group(1).strip().lower()
            text = re.sub(r'\s+', ' ', m.group(2)).strip()
            nm   = _NAME_RE.search(text)
            if nm and not _NAME_BLACKLIST.match(nm.group(1).split()[0]):
                mailto_names[addr] = nm.group(1)

    def _pick_name(candidates: list[str]) -> str:
        for c in candidates:
            c = c.strip()
            if not c:
                continue
            first = c.split()[0]
            if _NAME_BLACKLIST.match(first):
                continue
            return c
        return ""

    lines = combined.splitlines()

    for email in contacts:
        email_l = email.lower()

        # Strategy 1
        if email_l in mailto_names:
            result[email] = mailto_names[email_l]
            continue

        name = ""
        for i, line in enumerate(lines):
            if email_l not in line.lower():
                continue

            # Strategy 2: same line, with email token removed to avoid false hits
            stripped = re.sub(re.escape(email_l), "", line, flags=re.IGNORECASE)
            name = _pick_name(_NAME_RE.findall(stripped))

            # Strategy 3: up to 3 lines above, stop if a different email appears
            if not name:
                for j in range(i - 1, max(i - 4, -1), -1):
                    above = lines[j]
                    if EMAIL_RE.search(above) and email_l not in above.lower():
                        break
                    name = _pick_name(_NAME_RE.findall(above))
                    if name:
                        break

            break  # only use the first occurrence of this email in the text

        result[email] = name

    return result


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

# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def categorize(text: str, html: str, country_cfg: dict) -> tuple[set[str], list[str], int]:
    hay = (text + " " + html[:250000]).lower()
    cats, reasons = set(), []
    score = 0
    weights = {"web_agency": 25, "wordpress": 25, "seo": 18,
               "communication": 18, "public_sector": 10, "ai_interest": 8}
    for cat, kws in country_cfg.get("keywords", {}).items():
        hits = [kw for kw in kws if kw.lower() in hay]
        if hits:
            cats.add(cat)
            score += weights.get(cat, 10)
            reasons.append(f"{cat}: " + ", ".join(hits[:4]))
    if any(x in hay for x in country_cfg.get("service_words", ["services", "customers", "clients", "case"])):
        score += 8
        reasons.append("has services/customers/cases language")
    if any(x in hay for x in country_cfg.get("support_words", ["support", "hosting", "maintenance"])):
        score += 6
        reasons.append("offers maintenance/support")
    agency_hits = [x for x in country_cfg.get("agency_words", []) if x.lower() in hay]
    if agency_hits:
        score += min(len(agency_hits) * 10, 20)
        reasons.append("agency language: " + ", ".join(agency_hits[:3]))
    neg_hits = [kw for kw in _CONTENT_NEG_KWS if kw in hay]
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
    if any(t in tech for t in ("Episerver", "Optimizely CMS")):
        return "Position BlueSearch as an AI search layer on top of Episerver/Optimizely — no rebuild needed."
    if "Umbraco" in tech:
        return "Offer BlueSearch as a plug-in AI search add-on for their Umbraco customer base."
    if "Sitecore" in tech:
        return "Offer BlueSearch as a native search replacement inside their Sitecore projects."
    if "Kentico" in tech:
        return "Integrate BlueSearch into their Kentico builds as an AI-powered site search."
    if "TYPO3" in tech:
        return "Add BlueSearch as a TYPO3 extension for AI search on client sites."
    if "Webflow" in tech or "Framer" in tech:
        return "BlueSearch embeds in Webflow/Framer sites with a single script tag — easy win for clients."
    if "HubSpot" in tech:
        return "Replace HubSpot's basic search with BlueSearch for smarter lead-capture on client portals."
    cats = set(cats) if cats else set()
    if "ecommerce" in cats:
        return "Upgrade their e-commerce clients' on-site search to AI-powered product discovery."
    if "communication" in cats or "public_sector" in cats:
        return "Focus on public-information sites: help visitors find answers across pages, PDFs and articles."
    return "General reseller angle: add AI-powered search to existing customer websites without rebuilding them."

# ---------------------------------------------------------------------------
# Tech detection
# ---------------------------------------------------------------------------

def detect_tech(html: str, soup: BeautifulSoup) -> set[str]:
    found: set[str] = set()
    h = html.lower()
    if "wp-content" in h or "wp-includes" in h:
        found.add("wordpress")
    if "woocommerce" in h:
        found.add("woocommerce")
    if "cdn.shopify.com" in h or "shopify.com/s/files" in h:
        found.add("shopify")
    if "webflow.io" in h or "framerusercontent.com" in h or 'data-wf-' in h:
        found.add("webflow")
    if "wix.com" in h or "wixsite.com" in h or "static.wixstatic" in h:
        found.add("wix")
    if "squarespace.com" in h:
        found.add("squarespace")
    if "framer.com" in h or "framerusercontent.com" in h:
        found.add("framer")
    if "elementor" in h:
        found.add("elementor")
    if "et-pb-" in h:
        found.add("divi")
    if "_next/static" in h or "next.js" in h:
        found.add("nextjs")
    if "gatsby" in h:
        found.add("gatsby")
    if "sites/default/files" in h and "drupal" in h:
        found.add("drupal")
    if "joomla" in h and ("/media/jui/" in h or "joomla!" in h):
        found.add("joomla")
    if "umbraco" in h:
        found.add("umbraco")
    if "typo3" in h:
        found.add("typo3")
    if "magento" in h or "mage/cookies" in h:
        found.add("magento")
    if "prestashop" in h:
        found.add("prestashop")
    if "shopware" in h:
        found.add("shopware")
    if "hs-scripts.com" in h or "hubspot.com" in h:
        found.add("hubspot")
    if "contentful" in h:
        found.add("contentful")
    if "storyblok" in h:
        found.add("storyblok")
    if "sanity.io" in h:
        found.add("sanity")
    if "craft" in h and "craftcms" in h:
        found.add("craftcms")
    return found

