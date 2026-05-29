"""site_agent.py -- Find content-heavy websites via Bing, measure size via sitemap,
and extract contacts from sites where a sitemap is found.

Pipeline per site:
  1. Bing search with queries from config/site_agent_queries.json
  2. Async fetch robots.txt -> locate sitemap URL
  3. Async parse sitemap (index + urlset) -> count pages
  4. If sitemap found: async crawl homepage + contact page -> extract emails/phones
  5. Upsert SiteLead to Firestore `site_leads/{lead_id}`
  6. Upsert each contact to `site_leads/{lead_id}/site_contacts/{contact_id}`

Producer/consumer pipeline: Bing producers (up to 5 concurrent) push URLs
into a queue the moment each search returns.  Site consumers (up to 20
concurrent) pull from the queue immediately -- sitemap reading starts as
soon as the first Bing result lands, with no waiting for all searches to finish.
No existing files are modified.

Usage:
    python app/site_agent.py --countries NO
    python app/site_agent.py --countries NO,SE,DK --max-results 50
    python app/site_agent.py --countries NO --min-pages 100 --dry-run
    python app/site_agent.py --countries ALL --workers 20
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp

import _pathsetup  # noqa: F401

from functions.utils import (
    BROWSER_UA,
    company_from_domain,
    domain_of,
    extract_contacts,
    is_blocked,
    is_global_tld,
    load_country_configs,
    load_lines,
    normalize_url,
    pair_names_to_contacts,
    pair_phones_to_contacts,
    tld_accepted_for,
)
from functions.models import lead_id_from_url

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SITE_AGENT_CONFIG_PATH = Path(__file__).parent.parent / "config" / "site_agent_queries.json"
BLOCKLIST_PATH         = Path(__file__).parent.parent / "config" / "site_agent_blocklist.txt"
COLLECTION_DEFAULT     = "site_leads"
WORKERS_DEFAULT        = 20

_HTTP_HEADERS = {
    "User-Agent":      BROWSER_UA,
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def load_site_config(path: Path = SITE_AGENT_CONFIG_PATH) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Site agent config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_blocklist(path: Path = BLOCKLIST_PATH) -> set[str]:
    patterns: set[str] = set()
    for line in load_lines(path):
        if line.upper().startswith("CONTENT NEGATIVE"):
            break
        patterns.add(line.lower())
    return patterns


def _is_main_page(url: str) -> bool:
    """Return True if the URL looks like a homepage (root or short locale root).

    Accepted:
      https://example.com          path = ""
      https://example.com/         path = "/"
      https://example.com/en       path = "/en"   (1 short segment)
      https://example.com/nb-no    path = "/nb-no"

    Rejected:
      https://example.com/products/shoes
      https://example.com/blog/2024/post-title
    """
    path = urlparse(url).path.rstrip("/")
    if not path:
        return True
    parts = [p for p in path.split("/") if p]
    # Allow exactly one segment that looks like a locale code (≤6 chars)
    if len(parts) == 1 and len(parts[0]) <= 6:
        return True
    return False


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class SiteContact:
    contact_id:   str
    email:        str
    name:         str
    title:        str
    phone:        str
    lead_id:      str
    domain:       str
    website:      str
    country:      str
    country_name: str
    found_on:     str


@dataclass
class SiteLead:
    lead_id:       str
    domain:        str
    website:       str
    country:       str        # ISO code, or "*" for global TLDs (.com/.org/.net)
    country_name:  str
    company:       str
    title:         str
    description:   str
    page_count:    int
    sitemap_url:   str
    sitemap_type:  str        # "index" | "urlset" | "none"
    source_query:  str
    crawled_at:    str
    query_category:      str       = ""
    sitemap_oldest_date: str       = ""          # oldest <lastmod> across all sitemaps
    sitemap_newest_date: str       = ""          # newest <lastmod> across all sitemaps
    platform:        str           = ""          # "woocommerce" | "shopify" | "wordpress" | ""
    sitemaps:        list          = field(default_factory=list)   # [{url, filename, lastmod}]
    target_types:    list[str]     = field(default_factory=list)
    keywords:        list[str]     = field(default_factory=list)
    contacts:        list[SiteContact] = field(default_factory=list, repr=False)


def _contact_id(email: str) -> str:
    return hashlib.sha1(email.lower().encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Keyword extraction
# ---------------------------------------------------------------------------

_STOPWORDS: frozenset[str] = frozenset({
    "the", "and", "for", "with", "from", "this", "that", "are", "was",
    "has", "have", "not", "but", "its", "our", "your", "their", "can",
    "will", "been", "more", "also", "all", "any", "one", "they",
    "og", "er", "til", "fra", "med", "det", "den", "som", "ved",
    "har", "ikke", "kan", "vil", "var", "men", "deg", "seg", "ett",
    "och", "inte", "att", "vid", "alla",
    "alle",
    "und", "mit", "das", "der", "die", "des", "dem", "als",
    "zur", "zum", "von", "aus", "bei", "eine", "einer", "nicht", "auch", "wird", "sind",
    "van", "voor", "het", "een", "zijn", "ook", "deze", "naar",
    "les", "des", "pour", "avec", "dans", "sur", "par", "une", "son",
    "ses", "leur", "aux", "pas", "est", "sont", "ont", "mais", "qui", "que", "tout", "plus",
})

_TOKEN_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _extract_keywords(
    source_query: str,
    title:        str,
    description:  str,
    company:      str,
    target_types: list[str],
) -> list[str]:
    """Build a deduplicated keyword list from search query + target types + page meta.

    Priority:
      1. Every word in source_query  -- explicitly chosen search terms
      2. target_types values         -- e.g. "municipality", "healthcare", "ecommerce"
      3. Significant words from title (up to 10)
      4. Significant words from description snippet (up to 8)
      5. Company name words (up to 5)
    """
    seen: set[str] = set()
    keywords: list[str] = []

    def _add(text: str, max_words: int = 0) -> None:
        clean = _TOKEN_RE.sub(" ", text.lower())
        count = 0
        for w in clean.split():
            if len(w) < 3 or w in _STOPWORDS or w.isdigit():
                continue
            if w not in seen:
                seen.add(w)
                keywords.append(w)
            count += 1
            if max_words and count >= max_words:
                break

    _add(source_query)
    for t in target_types:
        t = t.strip().lower()
        if t and t not in seen:
            seen.add(t)
            keywords.append(t)
    _add(title,             max_words=10)
    _add(description[:200], max_words=8)
    _add(company,           max_words=5)
    return keywords


# ---------------------------------------------------------------------------
# Bing search (sync -- runs in executor thread)
# ---------------------------------------------------------------------------

def _bing_search(query: str, max_results: int) -> list[str]:
    try:
        import search_runner
        return search_runner.bing_search(query, max_results)
    except Exception as exc:
        print(f"  [bing] error: {exc}")
        return []


def _brave_search(query: str, max_results: int, country_code: str = "") -> list[str]:
    try:
        import search_runner
        return search_runner.brave_search(query, max_results, country_code=country_code)
    except Exception as exc:
        print(f"  [brave] error: {exc}")
        return []


# ---------------------------------------------------------------------------
# Async HTTP helpers
# ---------------------------------------------------------------------------

# Bot UA for sitemap fetches — WordPress/Yoast serves raw XML to crawlers
_BOT_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

async def _async_get(session: aiohttp.ClientSession, url: str,
                     timeout: int = 15, xml: bool = False,
                     return_final_url: bool = False):
    headers = dict(_HTTP_HEADERS)
    if xml:
        headers["Accept"] = "application/xml,text/xml,*/*;q=0.8"
        headers["User-Agent"] = _BOT_UA
    try:
        async with session.get(
            url, headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=True, ssl=False,
        ) as resp:
            final_url = str(resp.url)
            if resp.status != 200:
                return ("", url) if return_final_url else ""
            raw = await resp.read()
            # Decompress if the server sent gzip without Content-Encoding header
            # (some hosts serve sitemap.xml.gz as application/xml without announcing it)
            if raw[:2] == b"\x1f\x8b":
                try:
                    import gzip as _gzip
                    raw = _gzip.decompress(raw)
                except Exception:
                    return ("", url) if return_final_url else ""
            text = raw.decode("utf-8", errors="replace")[:3_000_000]
            if xml:
                # Ignore Content-Type — many servers send text/html for valid XML.
                # Strip BOM (\ufeff) before checking, as lstrip() does not strip it.
                stripped = text.lstrip("\ufeff").lstrip()
                if not (stripped.startswith("<?xml")
                        or stripped.startswith("<sitemapindex")
                        or stripped.startswith("<urlset")):
                    return ("", url) if return_final_url else ""
            return (text, final_url) if return_final_url else text
    except Exception:
        return ("", url) if return_final_url else ""


# ---------------------------------------------------------------------------
# Sitemap helpers
# ---------------------------------------------------------------------------

_SM_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
_SITEMAP_PATHS = [
    # Standard / most common
    "/sitemap.xml", "/sitemaps.xml", "/sitemap_index.xml", "/sitemap-index.xml",
    "/sitemap1.xml",
    # Compressed variants (some servers serve pre-gzipped sitemaps)
    "/sitemap.xml.gz", "/sitemap_index.xml.gz",
    # WordPress / Yoast SEO
    "/wp-sitemap.xml",
    "/post-sitemap.xml", "/page-sitemap.xml", "/category-sitemap.xml",
    # Sub-directory conventions
    "/sitemaps/sitemap.xml", "/sitemaps/sitemap_index.xml",
    "/sitemap/sitemap.xml", "/sitemap/index.xml",
    # News publishers
    "/news-sitemap.xml", "/sitemap-news.xml",
    "/sitemap-articles.xml", "/sitemap_news.xml",
    "/artikkel-sitemap.xml",          # Nordic / Norwegian news
    "/feed/sitemap.xml", "/feeds/sitemap.xml",
]


def _parse_xml_safe(text: str) -> ET.Element | None:
    # Strip BOM (U+FEFF) — ElementTree rejects it with "not at start of entity"
    text = text.lstrip("\ufeff").lstrip("﻿")
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        # Strip ALL processing instructions (<?xml ...?>, <?xml-stylesheet ...?>, etc.)
        # before retrying.  Yoast sitemaps include a <?xml-stylesheet?> PI that some
        # builds of ElementTree reject, even though it is valid XML.
        cleaned = re.sub(r"<\?[^>]*?\?>", "", text).strip()
        try:
            return ET.fromstring(cleaned)
        except ET.ParseError:
            return None


def _count_urls(root: ET.Element) -> int:
    n = len(root.findall(f"{{{_SM_NS}}}url"))
    return n or len(root.findall("url"))


def _sm_filename(url: str) -> str:
    return url.rstrip("/").split("/")[-1] or url


def _index_entries(root: ET.Element) -> list[tuple[str, str]]:
    """Return (loc_url, lastmod) pairs from every <sitemap> entry in an index.

    IMPORTANT: never use `elem or fallback` with ElementTree elements.
    An element with no child nodes evaluates as falsy even when it exists,
    so `find(ns_loc) or find(loc)` silently drops valid results.
    Always use `is None` guards instead.
    """
    items = root.findall(f"{{{_SM_NS}}}sitemap")
    if not items:
        items = root.findall("sitemap")
    result = []
    for sm in items:
        loc = sm.find(f"{{{_SM_NS}}}loc")
        if loc is None:
            loc = sm.find("loc")
        lm = sm.find(f"{{{_SM_NS}}}lastmod")
        if lm is None:
            lm = sm.find("lastmod")
        url     = (loc.text or "").strip() if loc is not None else ""
        lastmod = (lm.text  or "").strip() if lm  is not None else ""
        if url:
            result.append((url, lastmod))
    return result


def _urlset_oldest_lastmod(root: ET.Element) -> str:
    lms = root.findall(f"{{{_SM_NS}}}url/{{{_SM_NS}}}lastmod")
    if not lms:
        lms = root.findall("url/lastmod")
    dates = [lm.text.strip() for lm in lms if lm.text]
    return min(dates) if dates else ""


def _urlset_newest_lastmod(root: ET.Element) -> str:
    lms = root.findall(f"{{{_SM_NS}}}url/{{{_SM_NS}}}lastmod")
    if not lms:
        lms = root.findall("url/lastmod")
    dates = [lm.text.strip() for lm in lms if lm.text]
    return max(dates) if dates else ""


def _detect_platform(found_url: str, sitemaps: list[dict]) -> str:
    """Detect CMS/e-commerce platform from sitemap URL patterns.

    Signals used (sitemap-only, no page HTML required):
      shopify     -- sub-sitemaps named sitemap_products_N.xml / sitemap_collections_N.xml
      woocommerce -- WordPress core sitemap (wp-sitemap) + product post-type sub-sitemap
      wordpress   -- WordPress core sitemap (wp-sitemap) without WooCommerce products
    Returns '' when no known platform is detected.
    """
    all_urls = [s.get("url", "").lower() for s in sitemaps]
    all_fns  = [s.get("filename", "").lower() for s in sitemaps]
    root_lc  = found_url.lower()

    # Shopify: fixed naming convention for product/collection sub-sitemaps
    if any("sitemap_products_" in fn or "sitemap_collections_" in fn for fn in all_fns):
        return "shopify"

    # WordPress family: identified by wp-sitemap in any URL
    if any("wp-sitemap" in u for u in all_urls) or "wp-sitemap" in root_lc:
        # WooCommerce adds a product post-type sub-sitemap
        if any("product" in fn for fn in all_fns):
            return "woocommerce"
        return "wordpress"

    return ""


def _detect_platform_from_html(html: str) -> str:
    """Detect CMS/platform from homepage HTML content.

    Used as a fallback when sitemap filenames give no signal (e.g. Episerver,
    Umbraco, Sitecore, Drupal).  Checks are ordered by confidence / specificity.
    Returns '' when no known platform is detected.
    """
    if not html:
        return ""
    h = html.lower()

    # Episerver / Optimizely Content Cloud (Swedish CMS, common in Scandinavia)
    if "/episerver/" in h or "data-epi-" in h or '"episerver"' in h:
        return "episerver"

    # Umbraco (.NET CMS)
    if "/umbraco/" in h or "umbraco.services" in h:
        return "umbraco"

    # Sitecore
    if "/-/media/" in h or "/sitecore/" in h or "sitecore.net" in h:
        return "sitecore"

    # Drupal
    if "drupal.settings" in h or '"drupal"' in h or "/sites/default/files/" in h:
        return "drupal"

    # TYPO3
    if "typo3" in h and ("/typo3/" in h or "typo3conf" in h):
        return "typo3"

    # Joomla
    if "/components/com_" in h or "joomla!" in h:
        return "joomla"

    # Angular (2+): ng-version attribute injected on root component element;
    # _nghost- / _ngcontent- are Angular's view-encapsulation hash attributes.
    # Also catches AngularJS 1.x via ng-app directive.
    if "ng-version=" in h or "_nghost-" in h or "_ngcontent-" in h or "ng-app=" in h:
        return "angular"

    # React: data-reactroot / data-reactid are React's DOM markers.
    # __NEXT_DATA__ is injected by Next.js (React SSR framework).
    # react.production.min.js covers sites that load React from CDN or expose bundle names.
    if ("data-reactroot" in h or "data-reactid" in h
            or "__next_data__" in h or "react.production.min.js" in h
            or "react-dom" in h):
        return "react"

    return ""


async def read_sitemap_async(session: aiohttp.ClientSession,
                             base_url: str,
                             debug: bool = False) -> tuple[int, str, str, list[dict], str, str, str]:
    """Return (total_url_count, first_sitemap_url, sitemap_type).

    Handles arbitrarily nested sitemap structures (index → sub-index → urlset):
    - Tries every candidate entry point (robots.txt + well-known paths)
    - Recurses into sub-sitemaps at any depth (capped at MAX_DEPTH=4)
    - Shared fetch budget (MAX_FETCHES=150) across ALL levels to prevent runaway
    - When budget is exhausted mid-index, extrapolates from the sample collected so far
    - Skips Google News sitemaps (only cover the last 2 days, not the full archive)
    - visited-URL set prevents any file being counted twice across all entry points
    """
    _MAX_FETCHES   = 150   # total HTTP fetches across all levels combined
    _MAX_DEPTH     = 4     # maximum sitemap nesting depth
    _SAMPLE_PER_LEVEL = 30 # max children fetched per index before extrapolating

    base = base_url.rstrip("/")
    visited: set[str]  = set()
    budget:       list[int] = [_MAX_FETCHES]  # mutable so nested closure can decrement it
    found_url:    str = ""
    found_type:   str = "none"
    all_sitemaps: list[dict] = []  # {url, filename, lastmod} per sitemap successfully fetched

    # Discover entry points from robots.txt — collect ALL Sitemap: lines.
    # News sites (e.g. vg.no) list the Google News sitemap first and the real
    # archive sitemap index on a later line; stopping at the first line misses it.
    robots_sitemaps: list[str] = []
    robots_text = await _async_get(session, f"{base}/robots.txt", timeout=10)
    for line in robots_text.splitlines():
        if line.strip().lower().startswith("sitemap:"):
            url = line.split(":", 1)[1].strip()
            if url and url not in robots_sitemaps:
                robots_sitemaps.append(url)

    # For each robots.txt sitemap URL that lives deep in a subdirectory
    # (e.g. /sitemaps/files/articles-48hrs.xml), also probe its parent and
    # grandparent directories for a sitemap index.  This catches sites like
    # vg.no whose archive index is at /sitemaps/sitemap_index.xml but whose
    # robots.txt only advertises the 48-hour news feed under /sitemaps/files/.
    _INDEX_NAMES = ("sitemap_index.xml", "sitemap-index.xml", "sitemap.xml")
    extra_from_robots: list[str] = []
    for _sm_url in robots_sitemaps:
        try:
            from urllib.parse import urlparse as _urlparse
            _path = _urlparse(_sm_url).path          # /sitemaps/files/articles-48hrs.xml
            _parent      = _path.rsplit("/", 1)[0]   # /sitemaps/files
            _grandparent = _parent.rsplit("/", 1)[0] # /sitemaps
            for _dir in (_parent, _grandparent):
                if _dir and _dir != "/":
                    for _name in _INDEX_NAMES:
                        _c = f"{base}{_dir}/{_name}"
                        if _c not in extra_from_robots and _c not in robots_sitemaps:
                            extra_from_robots.append(_c)
        except Exception:
            pass

    # robots.txt entries first (preserve order), then parent-dir guesses, then
    # well-known fallback paths.
    # (dedup: _count_sitemap will skip any URL already in visited)
    candidates = robots_sitemaps + extra_from_robots + [base + p for p in _SITEMAP_PATHS]

    def _dbg(msg: str) -> None:
        if debug:
            print(f"    [sitemap-dbg] {msg}")

    async def _count_sitemap(url: str, depth: int = 0, parent_lastmod: str = "") -> int:
        nonlocal found_url, found_type
        indent = "  " * depth
        if not url or url in visited:
            _dbg(f"{indent}SKIP (visited)  {url}")
            return 0
        if depth > _MAX_DEPTH or budget[0] <= 0:
            _dbg(f"{indent}SKIP (budget={budget[0]} depth={depth})  {url}")
            return 0

        visited.add(url)
        budget[0] -= 1

        text, final_url = await _async_get(session, url, timeout=15, xml=True, return_final_url=True)
        # If the server redirected us to a URL we already visited, it's a cycle — skip.
        if final_url != url:
            if final_url in visited:
                _dbg(f"{indent}REDIRECT-CYCLE  {url} -> {final_url} (already visited)")
                return 0
            visited.add(final_url)
        if not text:
            # Some CMS platforms (Episerver, Yoast) serve /sitemap.xml as a
            # human-readable HTML page that *lists* the real child XML sitemaps.
            # Fetch again without xml=True and extract <a href> links to .xml files.
            html = await _async_get(session, url, timeout=15, xml=False)
            _dbg(f"{indent}HTML-fallback: html_len={len(html)} for {url}")
            if html:
                import re as _re
                child_urls = []
                for m in _re.finditer(r"""href=["']((?:https?://[^"']*|/[^"']*)\.xml(?:\?[^"']*)?)["']""", html, _re.I):
                    child_url = m.group(1)
                    if not child_url.startswith("http"):
                        child_url = base + ("" if child_url.startswith("/") else "/") + child_url
                    if child_url not in visited and child_url not in child_urls:
                        child_urls.append(child_url)
                _dbg(f"{indent}HTML-fallback: found {len(child_urls)} .xml hrefs in {len(html)}-char HTML")
                if child_urls:
                    _dbg(f"{indent}HTML-sitemap-index: found {len(child_urls)} .xml links in HTML at {url}")
                    if not found_url:
                        found_url, found_type = url, "index"
                    sample_count = sampled = 0
                    for child_url in child_urls:
                        if sampled >= _SAMPLE_PER_LEVEL or budget[0] <= 0:
                            if sampled > 0:
                                sample_count = int((sample_count / sampled) * len(child_urls))
                            break
                        n = await _count_sitemap(child_url, depth + 1)
                        sample_count += n
                        sampled += 1
                    all_sitemaps.append({"url": url, "filename": _sm_filename(url),
                                         "lastmod": "", "lastmod_newest": "",
                                         "page_count": sample_count})
                    return sample_count

                # No .xml child links found — this HTML is likely a Yoast-rendered
                # urlset page showing actual page URLs (not sub-sitemap links).
                # Count distinct same-domain page hrefs as a proxy for page count.
                from urllib.parse import urlparse as _up
                _domain = _up(base).netloc
                _skip_ext = ('.xml', '.css', '.js', '.png', '.jpg', '.jpeg',
                             '.gif', '.svg', '.ico', '.pdf', '.woff', '.woff2')
                _skip_paths = ('#', 'mailto:', 'tel:', 'javascript:')
                page_links: set[str] = set()
                for m in _re.finditer(r'href=["\'](' + 'https?://' + _re.escape(_domain) + r'/[^"\']*)["\']', html, _re.I):
                    href = m.group(1).split('#')[0].rstrip('/')
                    if href and not any(href.endswith(e) for e in _skip_ext)                             and not any(p in href for p in _skip_paths)                             and href != base:
                        page_links.add(href)
                if page_links:
                    count = len(page_links)
                    _dbg(f"{indent}HTML-urlset: counted {count} page links at {url}")
                    if not found_url:
                        found_url, found_type = url, "urlset"
                    all_sitemaps.append({"url": url, "filename": _sm_filename(url),
                                         "lastmod": "", "lastmod_newest": "",
                                         "page_count": count})
                    return count

            _dbg(f"{indent}EMPTY (fetch returned nothing)  {url}")
            return 0
        root = _parse_xml_safe(text)
        if root is None:
            _dbg(f"{indent}PARSE-FAIL (trying HTML fallback)  {url}  peek={repr(text[:80])}")
            # text is non-empty but not valid XML — likely an HTML page (e.g. Yoast XSLT
            # rendered with browser UA, or a CMS that always serves HTML for sitemap URLs).
            # Try extracting <a href="*.xml"> links from the content we already have.
            import re as _re2
            child_urls = []
            for m in _re2.finditer(r"""href=["']((?:https?://[^"']*|/[^"']*)\.xml(?:\?[^"']*)?)["']""", text, _re2.I):
                child_url = m.group(1)
                if not child_url.startswith("http"):
                    child_url = base + ("" if child_url.startswith("/") else "/") + child_url
                if child_url not in visited and child_url not in child_urls:
                    child_urls.append(child_url)
            _dbg(f"{indent}HTML-from-xml-fetch: found {len(child_urls)} .xml hrefs")
            if child_urls:
                if not found_url:
                    found_url, found_type = url, "index"
                sample_count = sampled = 0
                for child_url in child_urls:
                    if sampled >= _SAMPLE_PER_LEVEL or budget[0] <= 0:
                        if sampled > 0:
                            sample_count = int((sample_count / sampled) * len(child_urls))
                        break
                    n = await _count_sitemap(child_url, depth + 1)
                    sample_count += n
                    sampled += 1
                all_sitemaps.append({"url": url, "filename": _sm_filename(url),
                                     "lastmod": "", "lastmod_newest": "",
                                     "page_count": sample_count})
                return sample_count
            return 0
        tag = root.tag.lower()
        _dbg(f"{indent}FETCH OK  tag={tag!r}  {url}")

        if "sitemapindex" in tag:
            # This is an index — could be root, sub-index, or archive index.
            # Recurse into every child (subject to budget + depth cap).
            if not found_url:
                found_url, found_type = url, "index"
            entries  = _index_entries(root)
            children = [(u, lm) for u, lm in entries if u not in visited]
            _dbg(f"{indent}  index: {len(entries)} entries, {len(children)} unvisited children")
            sample_count = sampled = 0
            for child_url, child_lm in children:
                if sampled >= _SAMPLE_PER_LEVEL or budget[0] <= 0:
                    # Extrapolate remaining children from the sample we have
                    if sampled > 0:
                        sample_count = int((sample_count / sampled) * len(children))
                    _dbg(f"{indent}  extrapolated → {sample_count:,} (sampled {sampled}/{len(children)})")
                    break
                n = await _count_sitemap(child_url, depth + 1, parent_lastmod=child_lm)
                sample_count += n
                sampled += 1
            # Append AFTER count is known so page_count is accurate
            all_sitemaps.append({"url": url, "filename": _sm_filename(url),
                                 "lastmod": parent_lastmod, "lastmod_newest": parent_lastmod,
                                 "page_count": sample_count})
            _dbg(f"{indent}  index total={sample_count:,}")
            return sample_count

        if "urlset" in tag:
            olm    = _urlset_oldest_lastmod(root) or parent_lastmod
            newest = _urlset_newest_lastmod(root) or parent_lastmod
            count  = _count_urls(root)
            all_sitemaps.append({"url": url, "filename": _sm_filename(url),
                                 "lastmod": olm, "lastmod_newest": newest,
                                 "page_count": count})
            if not found_url:
                found_url, found_type = url, "urlset"
            _dbg(f"{indent}urlset  count={count:,}  {url}")
            return count

        _dbg(f"{indent}UNKNOWN tag={tag!r}  {url}")
        return 0

    # Process every candidate entry point.
    # visited set ensures the same sitemap file is never counted twice even if
    # multiple entry points (robots.txt + /sitemap_index.xml) point to the same file.
    total = 0
    for candidate in candidates:
        total += await _count_sitemap(candidate)

    # Deduplicate by URL while preserving order
    seen_urls: set[str] = set()
    deduped = []
    for s in all_sitemaps:
        if s["url"] not in seen_urls:
            seen_urls.add(s["url"])
            deduped.append(s)
    oldest_date = min((s["lastmod"]        for s in deduped if s.get("lastmod")),        default="")
    newest_date = max((s["lastmod_newest"]  for s in deduped if s.get("lastmod_newest")), default="")
    platform    = _detect_platform(found_url, deduped)
    return total, found_url, found_type, deduped, oldest_date, newest_date, platform


# ---------------------------------------------------------------------------
# Page meta
# ---------------------------------------------------------------------------

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_DESC_RE_A = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{0,300})', re.IGNORECASE)
_DESC_RE_B = re.compile(
    r'<meta[^>]+content=["\']([^"\']{0,300})["\'][^>]+name=["\']description["\']', re.IGNORECASE)


def _extract_meta(html: str) -> tuple[str, str]:
    title = desc = ""
    m = _TITLE_RE.search(html)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()[:120]
    m = _DESC_RE_A.search(html) or _DESC_RE_B.search(html)
    if m:
        desc = re.sub(r"\s+", " ", m.group(1)).strip()[:300]
    return title, desc


# ---------------------------------------------------------------------------
# Contact scraping
# ---------------------------------------------------------------------------

_CONTACT_WORDS = [
    "contact", "kontakt", "kontakta", "about", "om-oss", "om oss",
    "team", "ansatte", "people", "staff", "company", "selskapet",
]
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)


def _find_contact_links(html: str, base_url: str, domain: str) -> list[str]:
    links: list[str] = []
    for m in _HREF_RE.finditer(html):
        href = m.group(1).strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(base_url, href).split("#")[0].split("?")[0]
        if domain_of(full) != domain:
            continue
        path = urlparse(full).path.lower()
        if any(w in path for w in _CONTACT_WORDS) and full not in links:
            links.append(full)
        if len(links) >= 3:
            break
    return links


async def scrape_contacts_async(
    session: aiohttp.ClientSession,
    website: str, domain: str, country: str, country_name: str, lead_id: str,
) -> list[SiteContact]:
    homepage_html = await _async_get(session, website, timeout=15)
    if not homepage_html:
        return []

    contact_links = _find_contact_links(homepage_html, website, domain)
    all_html = homepage_html
    page_html_map: dict[str, str] = {website: homepage_html}
    for link in contact_links:
        html = await _async_get(session, link, timeout=12)
        if html:
            all_html += "\n" + html
            page_html_map[link] = html

    combined_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", all_html))
    contacts: dict[str, str] = extract_contacts(all_html, combined_text)
    if not contacts:
        return []

    contact_phones = pair_phones_to_contacts(contacts, all_html + " " + combined_text, country)
    contact_names  = pair_names_to_contacts(contacts, all_html + " " + combined_text, all_html)

    def _found_on(email: str) -> str:
        for page_url, page_html in page_html_map.items():
            if email.lower() in page_html.lower():
                return page_url
        return website

    return [
        SiteContact(
            contact_id=_contact_id(email), email=email,
            name=contact_names.get(email, ""), title=contacts.get(email, ""),
            phone=contact_phones.get(email, ""), lead_id=lead_id,
            domain=domain, website=website, country=country,
            country_name=country_name, found_on=_found_on(email),
        )
        for email in sorted(contacts.keys())
    ]


# ---------------------------------------------------------------------------
# Per-site async worker
# ---------------------------------------------------------------------------

async def process_site_async(
    session:      aiohttp.ClientSession,
    url:          str,
    source_query: str,
    country:      str,
    country_name: str,
    min_pages:    int,
    target_types:   list[str] | None = None,
    query_category: str               = "",
) -> tuple[SiteLead | None, str]:
    """Return (lead, reason).  reason is "" on success, otherwise the exclusion cause."""
    try:
        website = normalize_url(url)
        domain  = domain_of(website)
        if not domain:
            return None, "url_error"
    except Exception:
        return None, "url_error"

    lead_id = lead_id_from_url(website)
    page_count, sitemap_url, sitemap_type, sitemaps, sitemap_oldest_date, sitemap_newest_date, platform = await read_sitemap_async(session, website)
    if min_pages > 0 and page_count < min_pages:
        print(f"    skip {domain}  pages={page_count} ({sitemap_type}) < min={min_pages}")
        return None, f"min_pages:{page_count}"

    homepage_html = await _async_get(session, website, timeout=15)
    title, description = _extract_meta(homepage_html) if homepage_html else ("", "")

    # HTML-based platform fallback (catches Episerver, Umbraco, Sitecore, Drupal, etc.)
    if not platform:
        platform = _detect_platform_from_html(homepage_html)

    contacts: list[SiteContact] = []
    if sitemap_type != "none" and homepage_html:
        contacts = await scrape_contacts_async(
            session, website, domain, country, country_name, lead_id)

    company  = company_from_domain(domain)
    ttypes   = target_types or []
    keywords = _extract_keywords(source_query, title, description, company, ttypes)

    return SiteLead(
        lead_id=lead_id, domain=domain, website=website,
        country=country, country_name=country_name, company=company,
        title=title, description=description, page_count=page_count,
        sitemap_url=sitemap_url, sitemap_type=sitemap_type,
        source_query=source_query,
        query_category=query_category,
        sitemap_oldest_date=sitemap_oldest_date,
        sitemap_newest_date=sitemap_newest_date,
        platform=platform,
        sitemaps=sitemaps,
        crawled_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        target_types=ttypes, keywords=keywords, contacts=contacts,
    ), ""


# ---------------------------------------------------------------------------
# Firestore
# ---------------------------------------------------------------------------

def _get_db(collection: str = COLLECTION_DEFAULT):
    try:
        from app.firestore_client import get_firestore
    except ImportError:
        from firestore_client import get_firestore
    db  = get_firestore()
    return db, db.collection(collection)


def preload_seen_domains(collection: str = COLLECTION_DEFAULT) -> set[str]:
    try:
        _, col = _get_db(collection)
        domains: set[str] = set()
        for doc in col.select(["domain"]).stream():
            d = doc.to_dict().get("domain", "")
            if d:
                domains.add(d.strip().lower())
        print(f"  [firebase] preloaded {len(domains)} existing domains from '{collection}'")
        return domains
    except Exception as exc:
        print(f"  [firebase] preload failed ({exc}) -- starting fresh")
        return set()


def upsert_site_lead(lead: SiteLead, collection: str = COLLECTION_DEFAULT) -> None:
    try:
        _, col = _get_db(collection)
        doc = asdict(lead)
        doc.pop("contacts", None)
        col.document(lead.lead_id).set(doc, merge=True)
        if lead.contacts:
            contacts_col = col.document(lead.lead_id).collection("site_contacts")
            for contact in lead.contacts:
                contacts_col.document(contact.contact_id).set(asdict(contact), merge=True)
    except Exception as exc:
        print(f"  [firebase] upsert error for {lead.domain}: {exc}")


EXCLUDED_COLLECTION_DEFAULT = "sites_excluded"


def preload_excluded_domains(collection: str = EXCLUDED_COLLECTION_DEFAULT) -> set[str]:
    """Load all previously excluded domains so we skip them without refetching."""
    try:
        _, col = _get_db(collection)
        domains: set[str] = set()
        for doc in col.select(["domain"]).stream():
            d = doc.to_dict().get("domain", "")
            if d:
                domains.add(d.strip().lower())
        print(f"  [firebase] preloaded {len(domains)} excluded domains from '{collection}'")
        return domains
    except Exception as exc:
        print(f"  [firebase] excluded preload failed ({exc}) -- continuing without")
        return set()


def upsert_site_excluded(
    domain:         str,
    website:        str,
    lead_id:        str,
    country:        str,
    reason:         str,
    page_count:     int   = 0,
    source_query:   str   = "",
    collection:     str   = EXCLUDED_COLLECTION_DEFAULT,
    query_category: str   = "",
) -> None:
    """Record a rejected site so it is skipped on future runs."""
    try:
        _, col = _get_db(collection)
        col.document(lead_id).set({
            "lead_id":        lead_id,
            "domain":         domain,
            "website":        website,
            "country":        country,
            "reason":         reason,
            "page_count":     page_count,
            "source_query":   source_query,
            "query_category": query_category,
            "excluded_at":    datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, merge=True)
    except Exception as exc:
        print(f"  [firebase] excluded upsert error for {domain}: {exc}")


# ---------------------------------------------------------------------------
# Async orchestrator
# ---------------------------------------------------------------------------

BING_WORKERS    = 5
_QUEUE_SENTINEL = None


async def _bing_query_async(
    semaphore:        asyncio.Semaphore,
    query:            str,
    max_results:      int,
    delay:            float,
    seen_domains:     set[str],
    excluded_domains: set[str],
    blocklist:        set[str],
    counters:         dict,
    queue:            asyncio.Queue,
    country:          str,
    country_configs:  dict,
    main_page_only:   bool = False,
    query_category:   str  = "",
) -> None:
    async with semaphore:
        loop = asyncio.get_running_loop()
        # Run Brave and Bing in parallel, then merge results (deduped, Brave first)
        brave_future = asyncio.wait_for(
            loop.run_in_executor(None, lambda: _brave_search(query, max_results, country)),
            timeout=45.0,
        )
        bing_future = asyncio.wait_for(
            loop.run_in_executor(None, lambda: _bing_search(query, max_results)),
            timeout=45.0,
        )
        brave_urls, bing_urls = [], []
        try:
            brave_urls = await brave_future
        except asyncio.TimeoutError:
            print(f"  [brave] timeout: {query!r}")
        try:
            bing_urls = await bing_future
        except asyncio.TimeoutError:
            print(f"  [bing] timeout: {query!r}")

        # Merge: Brave first, then Bing extras (skip already-seen URLs)
        seen_urls: set[str] = set()
        urls: list[str] = []
        for u in brave_urls + bing_urls:
            if u not in seen_urls:
                seen_urls.add(u)
                urls.append(u)
        urls = urls[:max_results * 2]  # allow extra since both engines contribute
        print(f"  [search] {query!r}  brave={len(brave_urls)} bing={len(bing_urls)} merged={len(urls)}")
    await asyncio.sleep(delay)

    for url in urls:
        d = domain_of(url)
        if not d:
            continue
        if main_page_only and not _is_main_page(url):
            counters["deep_link"] += 1
            continue
        if d in excluded_domains:
            counters["excl_skip"] += 1
            continue
        if d in seen_domains:
            counters["seen"] += 1
            continue
        if is_blocked(d, blocklist):
            counters["blocked"] += 1
            continue
        if not tld_accepted_for(d, country, country_configs):
            counters["tld_skip"] += 1
            continue
        effective_country = "*" if is_global_tld(d, country_configs) else country
        seen_domains.add(d)
        counters["queued"] += 1
        await queue.put((url, query, effective_country, query_category))


async def _run_country_full_async(
    queries:          list[tuple[str, str]],
    max_results:      int,
    delay:            float,
    blocklist:        set[str],
    seen_domains:     set[str],
    excluded_domains: set[str],
    country:          str,
    country_name:     str,
    min_pages:        int,
    workers:          int,
    no_firebase:      bool,
    dry_run:          bool,
    collection:       str,
    excl_collection:  str,
    country_configs:  dict,
    target_types:     list[str],
    main_page_only:   bool = False,
) -> list[SiteLead]:
    loop     = asyncio.get_running_loop()
    bing_sem = asyncio.Semaphore(BING_WORKERS)
    queue    = asyncio.Queue()
    counters = {"seen": 0, "blocked": 0, "tld_skip": 0, "deep_link": 0,
                "excl_skip": 0, "queued": 0, "done": 0, "excluded": 0}
    leads: list[SiteLead] = []
    failed_lead_writes: list[tuple]  = []   # (lead, collection) — retry after consumers drain
    failed_excl_writes: list[tuple]  = []   # upsert_site_excluded *args — retry after consumers drain

    connector = aiohttp.TCPConnector(limit=workers, limit_per_host=3, ssl=False)
    timeout   = aiohttp.ClientTimeout(total=30, connect=8)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:

        async def site_consumer() -> None:
            while True:
                item = await queue.get()
                try:
                    if item is _QUEUE_SENTINEL:
                        break
                    url, source_query, eff_country, query_category = item
                    eff_country_name = "Global" if eff_country == "*" else country_name
                    d = domain_of(url) or url

                    # Fast-skip: domain was excluded in a previous run or this run
                    if d in excluded_domains:
                        counters["excl_skip"] += 1
                        counters["done"] += 1
                        print(f"  [excl] skip {d}  (previously excluded)")
                        continue

                    excl_reason = ""
                    try:
                        lead, excl_reason = await asyncio.wait_for(
                            process_site_async(
                                session, url, source_query,
                                eff_country, eff_country_name,
                                min_pages, target_types,
                                query_category=query_category,
                            ),
                            timeout=120.0,
                        )
                    except Exception as exc:
                        counters["done"] += 1
                        n = counters["done"]
                        excl_reason = "error"
                        print(f"  [{n}] [consumer] error on {url}: {exc}")
                        _args = None
                        if not no_firebase and not dry_run:
                            try:
                                lead_id = lead_id_from_url(normalize_url(url))
                                _args = (d, url, lead_id, eff_country,
                                         excl_reason, 0, source_query, excl_collection,
                                         query_category)
                                await asyncio.wait_for(
                                    loop.run_in_executor(
                                        None, lambda a=_args: upsert_site_excluded(*a)
                                    ),
                                    timeout=12.0,
                                )
                            except (asyncio.TimeoutError, Exception) as exc:
                                if _args is not None:
                                    print(f"  [{n}] [excl-write] timeout/error — queued for retry")
                                    failed_excl_writes.append(_args)
                        excluded_domains.add(d)
                        counters["excluded"] += 1
                        continue

                    counters["done"] += 1
                    n = counters["done"]
                    if lead:
                        leads.append(lead)
                        c_info = f"  {len(lead.contacts)} contacts" if lead.contacts else ""
                        s_info = f"pages={lead.page_count} ({lead.sitemap_type})"
                        print(f"  [{n}] ok {lead.domain:<40} {s_info}{c_info}")
                        if not no_firebase and not dry_run:
                            try:
                                await asyncio.wait_for(
                                    loop.run_in_executor(
                                        None,
                                        lambda _l=lead, _c=collection: upsert_site_lead(_l, _c),
                                    ),
                                    timeout=12.0,
                                )
                            except (asyncio.TimeoutError, Exception) as exc:
                                print(f"  [{n}] [lead-write] timeout/error: {exc} — queued for retry")
                                failed_lead_writes.append((lead, collection))
                    else:
                        # Parse out real page_count if encoded in the reason string
                        stored_reason = excl_reason
                        stored_pages  = 0
                        if excl_reason.startswith("min_pages:"):
                            stored_pages  = int(excl_reason.split(":", 1)[1] or 0)
                            stored_reason = "min_pages"
                        print(f"  [{n}] -- excluded ({stored_reason})  {url}")
                        if excl_reason != "url_error":
                            _args = None
                            if not no_firebase and not dry_run:
                                try:
                                    lead_id = lead_id_from_url(normalize_url(url))
                                    _args = (d, url, lead_id, eff_country,
                                             stored_reason, stored_pages,
                                             source_query, excl_collection,
                                             query_category)
                                    await asyncio.wait_for(
                                        loop.run_in_executor(
                                            None, lambda a=_args: upsert_site_excluded(*a)
                                        ),
                                        timeout=12.0,
                                    )
                                except (asyncio.TimeoutError, Exception) as exc:
                                    if _args is not None:
                                        print(f"  [{n}] [excl-upsert] timeout/error — queued for retry")
                                        failed_excl_writes.append(_args)
                            excluded_domains.add(d)
                            counters["excluded"] += 1

                except Exception as exc:
                    print(f"  [consumer] unhandled error: {exc}")
                finally:
                    queue.task_done()

        consumer_tasks = [asyncio.create_task(site_consumer()) for _ in range(workers)]

        await asyncio.gather(*[
            _bing_query_async(
                bing_sem, q, max_results, delay,
                seen_domains, excluded_domains, blocklist, counters, queue,
                country, country_configs,
                main_page_only=main_page_only,
                query_category=cat,
            )
            for q, cat in queries
        ])

        deep_msg  = f", {counters['deep_link']} deep-links" if main_page_only else ""
        excl_msg  = f", {counters['excl_skip']} pre-excluded" if counters["excl_skip"] else ""
        excld_msg = f", {counters['excluded']} newly excluded" if counters["excluded"] else ""
        print(
            f"\n  [site_agent] skipped {counters['seen']} already-stored"
            f"{excl_msg}"
            f", {counters['blocked']} blocklisted"
            f", {counters['tld_skip']} wrong-TLD"
            f"{deep_msg}"
            f", {counters['queued']} queued"
            f"{excld_msg}"
        )
        for _ in range(workers):
            await queue.put(_QUEUE_SENTINEL)
        await asyncio.gather(*consumer_tasks)

        # Flush any writes that timed out / errored during processing.
        # Runs synchronously so nothing is lost before we return.
        if not no_firebase and not dry_run:
            if failed_lead_writes:
                print(f"\n  [firebase] flushing {len(failed_lead_writes)} lead write(s) that failed inline...")
                for _lead, _col in failed_lead_writes:
                    for attempt in range(3):
                        try:
                            upsert_site_lead(_lead, _col)
                            print(f"    ok {_lead.domain}")
                            break
                        except Exception as exc:
                            if attempt == 2:
                                print(f"    gave up {_lead.domain}: {exc}")
            if failed_excl_writes:
                print(f"  [firebase] flushing {len(failed_excl_writes)} excluded write(s) that failed inline...")
                for _eargs in failed_excl_writes:
                    for attempt in range(3):
                        try:
                            upsert_site_excluded(*_eargs)
                            break
                        except Exception as exc:
                            if attempt == 2:
                                print(f"    gave up {_eargs[0]}: {exc}")

    print(f"  [site_agent] {counters['done']} processed, {len(leads)} kept.\n")
    return leads


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run(
    countries:        list[str],
    max_results:      int   = 50,
    min_pages:        int   = 0,
    workers:          int   = WORKERS_DEFAULT,
    delay:            float = 1.5,
    no_firebase:      bool  = False,
    collection:       str   = COLLECTION_DEFAULT,
    excl_collection:  str   = "sites_excluded",
    dry_run:          bool  = False,
    category:         str | None = None,
    main_page_only:   bool  = False,
) -> list[SiteLead]:
    config          = load_site_config()
    blocklist       = load_blocklist()
    country_configs = load_country_configs()
    print(f"  [blocklist]  {len(blocklist)} patterns loaded")
    print(f"  [tld-filter] {len(country_configs)} country configs loaded")

    seen_domains:     set[str] = set()
    excluded_domains: set[str] = set()
    if not no_firebase:
        seen_domains     = preload_seen_domains(collection)
        excluded_domains = preload_excluded_domains(excl_collection)

    all_leads: list[SiteLead] = []

    for country_code in countries:
        country_code = country_code.upper()
        cfg = config.get(country_code)
        if not cfg:
            print(f"  [site_agent] No config for '{country_code}' -- skipping.")
            continue

        country_name  = cfg.get("name", country_code)
        country_min   = cfg.get("min_pages", 0)
        effective_min = max(min_pages, country_min)
        target_types  = cfg.get("target_types", [])

        # Support both flat `queries` list and categorised `query_categories` dict
        # queries is always list[tuple[str, str]] = (query_text, category_name)
        query_cats = cfg.get("query_categories")
        if query_cats:
            if category:
                cat_lower = category.lower()
                if cat_lower not in query_cats:
                    available = ", ".join(query_cats.keys())
                    print(f"  [site_agent] Category '{cat_lower}' not found for {country_code}. "
                          f"Available: {available}")
                    continue
                queries     = [(q, cat_lower) for q in query_cats[cat_lower]]
                cat_display = cat_lower
            else:
                queries     = [(q, cat) for cat, qs in query_cats.items() for q in qs]
                cat_display = "ALL (" + ", ".join(query_cats.keys()) + ")"
        else:
            queries     = [(q, "") for q in cfg.get("queries", [])]
            cat_display = "—"

        print(f"\n{'='*60}")
        print(f"  Country      : {country_name} ({country_code})")
        print(f"  Category     : {cat_display}")
        print(f"  Target types : {', '.join(target_types)}")
        print(f"  Queries      : {len(queries)} ({min(BING_WORKERS, len(queries))} parallel)")
        print(f"  Min pages    : {effective_min}")
        print(f"  Workers      : {workers}")
        print(f"{'='*60}")

        country_leads = asyncio.run(
            _run_country_full_async(
                queries          = queries,
                max_results      = max_results,
                delay            = delay,
                blocklist        = blocklist,
                seen_domains     = seen_domains,
                excluded_domains = excluded_domains,
                country          = country_code,
                country_name     = country_name,
                min_pages        = effective_min,
                workers          = workers,
                no_firebase      = no_firebase,
                dry_run          = dry_run,
                collection       = collection,
                excl_collection  = excl_collection,
                country_configs  = country_configs,
                target_types     = target_types,
                main_page_only   = main_page_only,
            )
        )

        all_leads.extend(country_leads)
        contacts_total = sum(len(l.contacts) for l in country_leads)
        print(
            f"\n  [site_agent] {country_code} done -- "
            f"{len(country_leads)} sites, {contacts_total} contacts stored."
        )

    print(f"\n{'='*60}")
    total_contacts = sum(len(l.contacts) for l in all_leads)
    print(
        f"  TOTAL: {len(all_leads)} sites | {total_contacts} contacts "
        f"across {len(countries)} country(-ies)"
    )
    print(f"{'='*60}\n")
    return all_leads


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> None:
    from dotenv import load_dotenv
    load_dotenv()

    p = argparse.ArgumentParser(
        description="Site Agent -- find content-heavy websites via Bing + sitemap + contacts"
    )
    p.add_argument("--countries",   default="NO",
                   help="Comma-separated ISO codes or ALL  (default: NO)")
    p.add_argument("--max-results", type=int, default=500, metavar="N",
                   help="Max Bing results per query  (default: 50)")
    p.add_argument("--min-pages",   type=int, default=0, metavar="N",
                   help="Minimum sitemap page count to keep a site  (default: 0)")
    p.add_argument("--workers",     type=int, default=WORKERS_DEFAULT, metavar="N",
                   help="Async concurrency limit  (default: 20)")
    p.add_argument("--delay",       type=float, default=1.5, metavar="SECS",
                   help="Seconds between Bing queries  (default: 1.5)")
    p.add_argument("--no-firebase", action="store_true",
                   help="Skip writing to Firestore")
    p.add_argument("--collection",      default=COLLECTION_DEFAULT, metavar="NAME",
                   help="Firestore collection  (default: site_leads)")
    p.add_argument("--excl-collection", default="sites_excluded", metavar="NAME",
                   help="Firestore collection for excluded sites  (default: sites_excluded)")
    p.add_argument("--dry-run",     action="store_true",
                   help="Print results without writing to Firestore")
    p.add_argument("--category",       default=None, metavar="NAME",
                   help="Run only this query category  e.g. company, shop, municipality")
    p.add_argument("--main-page-only", action="store_true",
                   help="Discard Bing results that are not homepage/root URLs")

    args = p.parse_args(argv)

    config = load_site_config()
    if args.countries.upper() == "ALL":
        countries = [k for k in config if not k.startswith("_")]
    else:
        countries = [c.strip().upper() for c in args.countries.split(",") if c.strip()]

    run(
        countries       = countries,
        max_results     = args.max_results,
        min_pages       = args.min_pages,
        workers         = args.workers,
        delay           = args.delay,
        no_firebase     = args.no_firebase,
        collection      = args.collection,
        excl_collection = args.excl_collection,
        dry_run         = args.dry_run,
        category        = args.category,
        main_page_only  = args.main_page_only,
    )


if __name__ == "__main__":
    main()