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

# Characters that signal a JSON-artifact or label suffix in a title/name field
# Straight quote, curly/smart quotes (“”‘’), JSON chars, operator chars
_CLEAN_BAD_CHARS = frozenset(';«_()&:\\/<>+=,|"\u201c\u201d\u2018\u2019{}')
# Scandinavian email-label prefixes: ' E-post', ' E ', ' E:', space-padded dash ' - '
_CLEAN_LABEL_RE  = re.compile(
    r'\s+[Ee]-[Pp]ost\b'          # E-post / e-post (Norwegian/Danish/Swedish)
    r'|\s+[Ee][:\s]'               # standalone E / e followed by : or space
    r'|\s+-\s'                     # space-padded dash separator
    # Phone label prefixes across languages (must be preceded by whitespace)
    r'|\s+[Tt][Ee][Ll]\.?(?=[\s:+\d]|$)'         # Tel / TEL (universal)
    r'|\s+[Tt][Ll][Ff]\.?(?=[\s:+\d]|$)'  # Tlf / TLF / Tlf. (NO/DK)
    r'|\s+[Tt]elefon\b'           # Telefon (NO/DK/SE/DE)
    r'|\s+[Tt]elephone\b'         # Telephone (EN/FR)
    r'|\s+[Pp]hone\b'             # Phone (EN)
    r'|\s+[Pp]uh\.?(?=[\s:+\d]|$)'  # Puh / Puh. (FI — Puhelin)
    r'|\s+[Mm]ob\.?(?=[\s:+\d]|$)'  # Mob / Mob. (mobile, universal)
    r'|\s+[Mm]obil\b'             # Mobil (NO/SE/DK)
    r'|\s+[Tt]él\.?(?=[\s:+\d]|$)'  # Tél / Tel. (FR)
    r'|\s+[Gg][Ss][Mm](?=[\s:+\d]|$)'
    r'|\s+[Mm]ailto:?'          # mailto: link artifact
    r'|-(?!(?-i:[A-Z])[a-zA-Z]{2,})',  # only keep hyphen if 3+ letter proper name follows (Anne-Sofie)  # GSM (universal)
    re.IGNORECASE,
)

# If the truncated result is itself just a label word, discard it entirely
_BARE_LABEL_RE = re.compile(
    r'^(?:[Tt][Ll][Ff]|[Tt][Ee][Ll]|[Gg][Ss][Mm]|[Mm]ob|[Mm]obil'
    r'|[Pp]uh|[Tt]él|[Pp]hone|[Tt]elefon|[Tt]elephone|[Mm]ailto|[Ee])\.?$',
    re.IGNORECASE,
)


def clean_str(value: str) -> str:
    """Truncate a name/title string at the earliest of:
      - a control character (ord < 32 or 127)
      - a JSON-artifact / operator char: \\ / | , < > + = " \u201c \u201d \u2018 \u2019 { }
      - a digit (phone numbers, codes)
      - a label prefix: E-post, Tlf/Tel/Phone/Mob/GSM/... (all languages), ' - '
    Truncates at the position of the trigger — never discards content before it.
    Hyphenated names like Anne-Sofie are preserved.
    """
    # Decode HTML entities (&amp; → &, &lt; → <, etc.) before scanning
    import html as _html
    value = _html.unescape(value)
    cut = len(value)
    # 1. Char-by-char: control chars, JSON-artifact / operator chars, digits
    for i, c in enumerate(value):
        if ord(c) < 32 or ord(c) == 127 or c in _CLEAN_BAD_CHARS or c.isdigit():
            cut = i
            break
    # 2. Regex: label prefixes (may fire earlier than a bare + in phone number)
    m = _CLEAN_LABEL_RE.search(value)
    if m and m.start() < cut:
        cut = m.start()
    result = value[:cut].strip()
    # If truncation leaves only a bare label word (Tlf, Tel, GSM …), discard it
    if _BARE_LABEL_RE.match(result):
        return ''
    return result


# ---------------------------------------------------------------------------
# Country normalisation
# ---------------------------------------------------------------------------

ISO_TO_CC = {"GB": "UK", "AU": "AU", "NZ": "NZ", "IN": "IN",
             "NO": "NO", "SE": "SE", "DK": "DK", "FI": "FI",
             "IE": "IE", "ZA": "ZA", "DE": "DE", "FR": "FR",
             "BE": "BE", "NL": "NL", "ES": "ES", "IT": "IT"}


def resolve_country(lead: dict) -> str:
    """Best available country code — location_country > ai_country > country.
    ISO codes normalised via ISO_TO_CC (e.g. GB → UK).
    """
    raw = (lead.get('location_country') or
           lead.get('ai_country') or
           lead.get('country') or '').upper().strip()
    return ISO_TO_CC.get(raw, raw)


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
    bl = Path(__file__).parent.parent.parent / "config" / "blocklist_domains.txt"
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

def normalize_url(url: str, homepage_only: bool = True) -> str:
    """Normalize URL. When homepage_only=True (default), strip path to root domain.

    Also sanitises trailing punctuation (colons, dots, semicolons) that appear in
    Bing/Brave search results and would produce malformed URLs.
    Pass homepage_only=False only when you explicitly want to preserve the path.
    """
    url = url.strip().rstrip(":.;,")   # remove trailing punctuation first
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    netloc = parsed.netloc.rstrip(":")  # strip dangling colon from netloc (no port)
    if not netloc:
        return ""
    if homepage_only:
        return f"{parsed.scheme}://{netloc}/"
    path = parsed.path or "/"
    return f"{parsed.scheme}://{netloc}{path}"


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
    """Return the country code whose country-specific TLD matches *domain*, or None."""
    domain_l = domain.lower()
    for code in countries:
        for tld in configs.get(code, {}).get("tlds", []):
            if domain_l.endswith(tld):
                return code
    return None


_UNIVERSAL_TLDS = {".com", ".org", ".net"}  # default fallback; overridden by load_global_tlds()


def load_global_tlds(configs: dict | None = None) -> set[str]:
    """Return the set of global/universal TLDs from config.

    Reads the top-level ``global_tlds`` list from *countries.json*.
    Falls back to the hard-coded ``_UNIVERSAL_TLDS`` set if the key is absent.
    """
    if configs is None:
        try:
            configs = load_country_configs()
        except Exception:
            configs = {}
    raw = configs.get("global_tlds", [])
    return set(raw) if raw else set(_UNIVERSAL_TLDS)


def is_global_tld(domain: str, configs: dict | None = None) -> bool:
    """Return True if *domain* ends with one of the configured global TLDs."""
    gtlds = load_global_tlds(configs)
    domain_l = domain.lower()
    return any(domain_l.endswith(t) for t in gtlds)


def tld_accepted_for(domain: str, country_code: str, configs: dict) -> bool:
    """Return True if *domain*'s TLD is in the accepted_tlds list for *country_code*.

    Global TLDs (.com / .org / .net by default, configurable via countries.json
    ``global_tlds`` key) are always accepted for every country and are never
    blocked regardless of what the per-country accepted_tlds list contains.

    Falls back to True when accepted_tlds is not configured (backward-compatible).
    """
    domain_l = domain.lower()
    # Global TLDs are always OK — no need to list them per country
    if is_global_tld(domain_l, configs):
        return True
    accepted = configs.get(country_code, {}).get("accepted_tlds", [])
    if not accepted:
        return True  # no config → accept all (safe default)
    return any(domain_l.endswith(tld) for tld in accepted)

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
    # Use "\n" as separator so block elements produce line breaks that
    # pair_phones_to_contacts / pair_names_to_contacts can split on.
    # Collapse runs of spaces/tabs but KEEP newlines so line structure survives.
    raw = soup.get_text("\n", strip=True)
    raw = re.sub(r"[^\S\n]+", " ", raw)   # collapse spaces/tabs, preserve \n
    raw = re.sub(r"\n{3,}", "\n\n", raw)  # max two consecutive blank lines
    return raw[:120_000]


def extract_contacts(html: str, text: str) -> dict[str, str]:
    """Return {email: title} — title is best-effort from surrounding text."""
    # Decode \uXXXX escapes (e.g. \u003e → >) before email extraction.
    # These appear when Next.js JSON-escapes angle brackets for XSS safety.
    def _decode_unicode_escapes(s: str) -> str:
        return re.sub(r'\\u([0-9a-fA-F]{4})',
                      lambda m: chr(int(m.group(1), 16)), s)
    combined = _decode_unicode_escapes(html) + " " + _decode_unicode_escapes(text)
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
        # Reject unicode-escape artifacts: "u003e", "u003c", "u0026" etc.
        # These leak in when \uXXXX sequences lose their backslash.
        if re.search(r'u00[0-9a-f]{2}', local_part, re.IGNORECASE):
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
                title = re.sub(r"\s+", " ", title).strip()
                # Stop before any email address or digit-heavy phone string
                # (the title window can spill into "name@domain +358 …")
                at_pos = title.find("@")
                if at_pos != -1:
                    title = title[:at_pos].rstrip(" .,;")
                title = title[:60]
        contacts[e] = clean_str(title)
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

    Strategy A — character window (primary):
      For every occurrence of the email in combined, scan ±400 chars for a phone
      number.  Picks the candidate closest to the email (by character distance).
      Stops early if another email sits between the candidate phone and this email.

    Strategy B — line-based fallback:
      Splits combined into lines and checks:
        1. Same line as the email.
        2. Up to 5 lines below — stops if a *different* email appears.
        3. Up to 5 lines above — stops if a *different* email appears.

    All occurrences of the email are tried (not just the first).
    """
    combined_l = combined.lower()
    lines      = combined.splitlines()
    result: dict[str, str] = {}

    for email in contacts:
        email_l = email.lower()
        phone   = ""

        # ------------------------------------------------------------------
        # Strategy A: character-window scan (works on minified HTML too)
        # ------------------------------------------------------------------
        pos = 0
        while not phone:
            idx = combined_l.find(email_l, pos)
            if idx == -1:
                break
            pos = idx + 1

            # Collect all phone candidates in window; keep the closest one
            # that has no foreign email sitting between it and our email.
            window_start = max(0, idx - 400)
            window_end   = min(len(combined), idx + len(email_l) + 400)
            window       = combined[window_start: window_end]
            window_l     = window.lower()
            email_pos_in_w = idx - window_start  # position of email in window

            best_phone    = ""
            best_dist     = 999999
            for m in GENERIC_PHONE_RE.finditer(window):
                parsed = _parse_phone(m.group(), country)
                if not parsed:
                    continue
                phone_pos = m.start()
                dist = abs(phone_pos - email_pos_in_w)
                if dist >= best_dist:
                    continue
                # Make sure no *other* email sits between phone and this email
                lo = min(phone_pos, email_pos_in_w)
                hi = max(phone_pos, email_pos_in_w)
                snippet = window_l[lo:hi]
                other_emails = [e for e in EMAIL_RE.findall(snippet)
                                if e.lower() != email_l]
                if other_emails:
                    continue
                best_phone = parsed
                best_dist  = dist

            if best_phone:
                phone = best_phone
                break

        # ------------------------------------------------------------------
        # Strategy B: line-based fallback (cleaner text, longer range)
        # ------------------------------------------------------------------
        if not phone:
            for i, line in enumerate(lines):
                if email_l not in line.lower():
                    continue

                # Pass 1: same line
                for m in GENERIC_PHONE_RE.findall(line):
                    parsed = _parse_phone(m, country)
                    if parsed:
                        phone = parsed
                        break

                # Pass 2: up to 5 lines below
                if not phone:
                    for delta in range(1, 6):
                        j = i + delta
                        if j >= len(lines):
                            break
                        neighbour = lines[j]
                        if EMAIL_RE.search(neighbour) and email_l not in neighbour.lower():
                            break
                        for m in GENERIC_PHONE_RE.findall(neighbour):
                            parsed = _parse_phone(m, country)
                            if parsed:
                                phone = parsed
                                break
                        if phone:
                            break

                # Pass 3: up to 5 lines above
                if not phone:
                    for delta in range(1, 6):
                        j = i - delta
                        if j < 0:
                            break
                        neighbour = lines[j]
                        if EMAIL_RE.search(neighbour) and email_l not in neighbour.lower():
                            break
                        for m in GENERIC_PHONE_RE.findall(neighbour):
                            parsed = _parse_phone(m, country)
                            if parsed:
                                phone = parsed
                                break
                        if phone:
                            break

                if phone:
                    break

        result[email] = phone
    return result


def pair_names_to_contacts(
    contacts: dict[str, str],
    combined: str,
    html: str = "",
) -> dict[str, str]:
    """Return {email: person_name_or_empty}.

    Strategies in descending reliability (tried in order; first match wins):
    0. Heading tag (<h1>–<h4>) within 600 chars before the email in raw HTML —
       handles structured person-card layouts (e.g. h3 → title → email → phone).
    1. <a href="mailto:EMAIL">Name Text</a> anchor text.
    2. Same line as the email (email token stripped).
    3. Up to 6 lines above the email line (stops at a different email address).
    4. Up to 3 lines below the email line.

    All occurrences of the email in the text are tried (not just the first) so
    that a person-card section further down the page wins over an introductory
    mention that has no name nearby.
    """
    result: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Strategy 0: heading tag near the email in raw HTML
    # Handles layouts like: <h3>Hanna Masalin</h3>…<p>hanna@…</p>
    # ------------------------------------------------------------------
    heading_names: dict[str, str] = {}
    if html:
        _heading_re = re.compile(
            r'<h[1-4][^>]*>(.*?)</h[1-4]>', re.IGNORECASE | re.DOTALL
        )
        _strip_tags = re.compile(r'<[^>]+>')
        for email in contacts:
            email_l  = email.lower()
            html_l   = html.lower()
            pos      = 0
            best     = ""
            while True:
                idx = html_l.find(email_l, pos)
                if idx == -1:
                    break
                # Search for the last heading within 600 chars before this email
                window = html[max(0, idx - 600): idx]
                for hm in reversed(list(_heading_re.finditer(window))):
                    raw_h = _strip_tags.sub("", hm.group(1))
                    raw_h = re.sub(r'\s+', ' ', raw_h).strip()
                    nm    = _NAME_RE.search(raw_h)
                    if nm and not _NAME_BLACKLIST.match(nm.group(1).split()[0]):
                        best = nm.group(1)
                        break
                if best:
                    break
                pos = idx + 1
            if best:
                heading_names[email.lower()] = best

    # ------------------------------------------------------------------
    # Strategy 1: mailto anchor text
    # ------------------------------------------------------------------
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

    # Prefer the visible-text section for line-based strategies — it has cleaner
    # line structure than raw HTML which buries emails inside tags.
    text_only   = combined[len(html):].lstrip() if html else combined
    search_text = text_only if text_only.strip() else combined
    lines       = search_text.splitlines()

    for email in contacts:
        email_l = email.lower()

        # Strategy 0
        if email_l in heading_names:
            result[email] = heading_names[email_l]
            continue

        # Strategy 1
        if email_l in mailto_names:
            result[email] = mailto_names[email_l]
            continue

        # Strategies 2–4: line-based, try ALL occurrences (not just first)
        name = ""
        for i, line in enumerate(lines):
            if email_l not in line.lower():
                continue

            # Strategy 2: same line, email token removed
            stripped = re.sub(re.escape(email_l), "", line, flags=re.IGNORECASE)
            candidate = _pick_name(_NAME_RE.findall(stripped))

            # Strategy 3: up to 6 lines above
            if not candidate:
                for j in range(i - 1, max(i - 7, -1), -1):
                    above = lines[j]
                    if EMAIL_RE.search(above) and email_l not in above.lower():
                        break
                    candidate = _pick_name(_NAME_RE.findall(above))
                    if candidate:
                        break

            # Strategy 4: up to 3 lines below
            if not candidate:
                for j in range(i + 1, min(i + 4, len(lines))):
                    below = lines[j]
                    if EMAIL_RE.search(below) and email_l not in below.lower():
                        break
                    candidate = _pick_name(_NAME_RE.findall(below))
                    if candidate:
                        break

            if candidate:
                name = candidate
                break   # found a name — no need to check other occurrences
            # else: this occurrence had no name nearby — try the next one

        result[email] = clean_contact_name(name, email)

    # -----------------------------------------------------------------------
    # Deduplication pass: if the same name was assigned to multiple emails,
    # keep it only for the email whose local part best matches the name.
    # For all other emails that got the same name, clear it to "".
    # (Prevents one person's name bleeding onto unrelated contacts — e.g.
    # grid.no/kontakt where all emails got "Henning Gustavsen".)
    # -----------------------------------------------------------------------
    from collections import defaultdict
    name_to_emails: dict[str, list[str]] = defaultdict(list)
    for email, name in result.items():
        if name:
            name_to_emails[name].append(email)

    for name, emails in name_to_emails.items():
        if len(emails) < 2:
            continue
        # Score each email: how many name tokens appear in its local part?
        def _score(email: str) -> int:
            local = email.split("@", 1)[0].lower()
            tokens = [t.lower() for t in re.split(r"[\s.\-_]+", name) if len(t) >= 3]
            return sum(1 for t in tokens if t in local or local in t)
        scored = sorted(emails, key=_score, reverse=True)
        best_score = _score(scored[0])
        # Clear the name for all emails that don't match as well as the best
        for email in scored[1:]:
            if _score(email) < best_score:
                result[email] = ""

    return result




# ---------------------------------------------------------------------------
# Contact name validation
# ---------------------------------------------------------------------------

# Generic email local parts that carry no personal-name information.
_GENERIC_LOCALS = re.compile(
    r'^(info|post|kontakt|contact|hei|hello|support|sales|hjelp|help|'
    r'noreply|no-reply|webmaster|admin|office|firma|company|mail|epost|'
    r'e-post|booking|post|redaksjon|redaction|service|team|web|digital)$',
    re.IGNORECASE,
)


def email_matches_name(email: str, name: str) -> bool:
    """Check if the name field is consistent with the email local part.

    Returns True if:
    - Name is empty (cannot check — benefit of doubt)
    - Email local part is generic/role (info@, sales@, etc.) — cannot check
    - Email local part is a hex/numeric hash — cannot check
    - At least one email token matches a name token (exact, initial, or substring)

    Returns False only when the email looks personal AND no name token
    aligns with any email token.

    Examples:
      john.smith@co.no  + "John Smith"       → True  (both tokens match)
      j.smith@co.no     + "John Smith"       → True  (j = initial of John)
      jsmith@co.no      + "John Smith"       → True  (jsmith contains smith)
      a.rolfsjord@co.no + "Anne-Sofie R."    → True  (a = initial, rolfsjord matches)
      john@co.no        + "Jane Doe"         → False (john ≠ jane/doe)
      info@co.no        + "John Smith"       → True  (generic — skip)
      12a3f9@co.no      + "John Smith"       → True  (hash — skip)
    """
    # Empty name — cannot validate
    if not name or not name.strip():
        return True

    if not email or "@" not in email:
        return True

    local = email.split("@")[0].lower().strip()

    # Generic / role email — cannot validate against a person name
    if _GENERIC_LOCALS.match(local):
        return True

    # Hash / numeric local part — cannot validate
    if re.fullmatch(r"[0-9a-f\-]{8,}", local) or re.fullmatch(r"[0-9]+", local):
        return True

    # Tokenise local part: split on . _ - and drop pure-digit tokens
    email_tokens = [t for t in re.split(r"[._\-]", local) if t and not t.isdigit()]
    if not email_tokens:
        return True

    # Tokenise name: split on space, hyphen, dot; lowercase; min 2 chars
    name_tokens = [t.lower() for t in re.split(r"[\s\-\.]+", name) if len(t) >= 2]
    if not name_tokens:
        return True

    # Check each email token against all name tokens
    for et in email_tokens:
        for nt in name_tokens:
            # Exact match
            if et == nt:
                return True
            # et is a single initial matching start of name token (e.g. j → john)
            if len(et) == 1 and nt.startswith(et):
                return True
            # et is multiple initials (e.g. as → anne-sofie — checks first letter of each token)
            if len(et) >= 2 and all(c == name_tokens[i][0] for i, c in enumerate(et)
                                    if i < len(name_tokens) and name_tokens[i]):
                return True
            # et is contained in nt or nt is contained in et (min 4 chars to avoid false positives)
            if len(et) >= 4 and (et in nt or nt in et):
                return True

    return False



def clean_contact_name(name: str, email: str) -> str:
    """Return a sanitised contact name, or "" if the name looks wrong.

    Rules (applied in order):
    1. Empty / too-short names → ""
    2. Name contains "@" → it is an email address, not a name → ""
    3. Name IS the email address → ""
    4. Name contains a URL → ""
    5. Name contains HTML/Unicode-escape artefacts (u003e etc.) → ""
    6. Name is implausibly long (> 80 chars) → ""
    7. For personal-looking email local parts (not a generic keyword):
       check that at least one name token (≥3 chars) appears as a
       substring of the local part or vice-versa.  If no overlap → ""
    """
    name = name.strip()
    if not name or len(name) < 2:
        return ""
    if "@" in name:
        return ""
    if name.lower() == email.lower():
        return ""
    if re.search(r'https?://', name, re.IGNORECASE):
        return ""
    if re.search(r'u00[0-9a-fA-F]{2}', name):
        return ""
    if len(name) > 80:
        return ""

    # Personal-email check: only applies when local part looks like a name
    local = email.split("@", 1)[0].lower()
    if not _GENERIC_LOCALS.match(local):
        # Tokenise both sides; require at least one ≥3-char overlap
        name_tokens = [t.lower() for t in re.split(r'[\s.\-_]+', name) if len(t) >= 3]
        local_tokens = re.split(r'[.\-_]+', local)
        if name_tokens and not any(
            nt in lt or lt in nt
            for nt in name_tokens
            for lt in local_tokens
        ):
            return ""

    return name



def normalize_phone_list(phones_str: str) -> str:
    """Clean a comma-separated phone string (email_phones or phones field).

    - Strips whitespace from each entry.
    - Removes entries that are empty or pure punctuation.
    - Deduplicates: if the same number appears more than once (across any
      position) the later occurrence is cleared to "" so the parallel
      alignment with the email list is preserved.
    - Strips trailing empty slots (keeps internal ones for alignment).
    """
    if not phones_str:
        return ""
    parts = [p.strip() for p in phones_str.split(",")]
    seen: set[str] = set()
    deduped: list[str] = []
    for p in parts:
        if p and p in seen:
            deduped.append("")        # duplicate — keep slot, clear value
        else:
            deduped.append(p)
            if p:
                seen.add(p)
    # Strip trailing empty slots
    while deduped and not deduped[-1]:
        deduped.pop()
    return ", ".join(deduped)

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
               "communication": 15, "public_sector": 8, "ai_interest": 8,
               "smb_focus": 12, "care_plan": 15}
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
    # Check negative keywords against VISIBLE TEXT only (not raw html) and
    # require >=2 occurrences. A single mention is typically a client reference
    # (e.g. 'Lucky Sushi' or 'Sjakk-Matt frisør' on a web agency portfolio).
    # A site that IS a restaurant/hairdresser/etc. uses the word many times.
    _text_low = text.lower()
    # Adult/explicit terms: one occurrence is enough — unambiguous
    _ADULT_CONTENT_KWS = {
        "porn", "pornhub", "pornography", "xvideos", "xhamster", "redtube",
        "youporn", "brazzers", "onlyfans", "chaturbate", "cam4", "livecam",
        "livejasmin", "webcam girls", "webcam sex", "camgirl", "camsite",
        "adult entertainment", "adult content", "adult film", "adult video",
        "erotic", "erotica", "erotik", "erotisch", "erotique",
        "erotyczny", "erotisk", "sexfilm", "sexvideo", "sex chat",
        "sexting", "escortservice", "escort service", "escort girl",
        "escort girls", "escorts", "incall", "outcall", "stripclub",
        "strip club", "lapdance", "peepshow", "striptease", "hentai",
        "anime porn", "milf", "fetish", "bdsm", "bondage", "dominatrix",
        "nudity", "nude", "naked", "naughty", "nsfw", "playboy",
        "penthouse", "hustler",
    }
    adult_hits = [kw for kw in _ADULT_CONTENT_KWS if kw in _text_low]
    if adult_hits:
        score -= 90
        reasons.append(f"ADULT-CONTENT penalty ({', '.join(adult_hits[:3])}): -90")

    # All other negative keywords: require ≥2 occurrences (avoid false positives
    # from single client-name mentions like "Sushi Bar" on an agency portfolio).
    neg_hits = [kw for kw in _CONTENT_NEG_KWS if kw not in _ADULT_CONTENT_KWS and _text_low.count(kw) >= 2]
    if neg_hits:
        penalty = min(len(neg_hits) * 30, 90)
        score -= penalty
        reasons.append(f"NON-AGENCY penalty ({', '.join(neg_hits[:3])}): -{penalty}")

    # --- Core-signal gate ---------------------------------------------------
    # A site must have at least ONE web_agency keyword OR one agency_words hit
    # to be considered a real agency candidate.  Without that gate, banks,
    # telcos, etc. can score highly via seo + communication + ai + services.
    has_agency_kw   = "web_agency" in cats
    has_agency_lang = any(
        x.lower() in hay for x in country_cfg.get("agency_words", [])
    )
    if not has_agency_kw and not has_agency_lang:
        if score > 35:
            reasons.append("core-signal cap applied (no web_agency keyword or agency language)")
            score = 35

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

