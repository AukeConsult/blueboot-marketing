"""sitemap_reader.py — Self-contained SitemapReader for the Cloud Function.

Extracted from app/site_agent.py so it can be used inside functions-crm/
without any dependency on the app/ directory (which is not deployed).

Public API
----------
    from crm.sitemap_reader import SitemapReader

    async with aiohttp.ClientSession() as session:
        page_count, sitemap_url, sitemap_type, sitemaps, oldest, newest, platform \
            = await SitemapReader(session, "https://example.com").read()

Returns a 7-tuple identical to site_agent.read_sitemap_async().
"""
from __future__ import annotations

import asyncio
import gzip as _gzip
import io as _io
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

import aiohttp

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SM_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_BOT_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

_HTTP_HEADERS = {
    "User-Agent":      _BROWSER_UA,
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_XML_HEADERS = {**_HTTP_HEADERS, "User-Agent": _BOT_UA}

_MAX_BODY = 8_000_000
_MAX_TEXT = 3_000_000

_SITEMAP_PATHS = [
    "/sitemap.xml", "/sitemaps.xml", "/sitemap_index.xml", "/sitemap-index.xml",
    "/sitemap1.xml",
    "/sitemap.xml.gz", "/sitemap_index.xml.gz",
    "/wp-sitemap.xml",
    "/post-sitemap.xml", "/page-sitemap.xml", "/category-sitemap.xml",
    "/sitemaps/sitemap.xml", "/sitemaps/sitemap_index.xml",
    "/sitemap/sitemap.xml", "/sitemap/index.xml",
    "/news-sitemap.xml", "/sitemap-news.xml",
    "/sitemap-articles.xml", "/sitemap_news.xml",
    "/artikkel-sitemap.xml",
    "/feed/sitemap.xml", "/feeds/sitemap.xml",
]

# ---------------------------------------------------------------------------
# Bounded HTTP fetcher (xml-aware, never raises)
# ---------------------------------------------------------------------------

class _Fetcher:
    """Size-capped, never-raising GET with separate browser/bot UA per request type."""

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session

    async def get(self, url: str, *, timeout: float = 15.0, xml: bool = False,
                  return_final_url: bool = False):
        headers = dict(_XML_HEADERS if xml else _HTTP_HEADERS)
        if xml:
            headers.setdefault("Accept", "application/xml,text/xml,*/*;q=0.8")
        empty = ("", url) if return_final_url else ""
        try:
            async with self._session.get(
                url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=True, ssl=False,
            ) as resp:
                final_url = str(resp.url)
                if resp.status != 200:
                    return empty
                raw = await resp.content.read(_MAX_BODY + 1)
                if len(raw) > _MAX_BODY:
                    raw = raw[:_MAX_BODY]
                if raw[:2] == b"\x1f\x8b":
                    try:
                        with _gzip.GzipFile(fileobj=_io.BytesIO(raw)) as gz:
                            raw = gz.read(_MAX_BODY)
                    except Exception:
                        return empty
                text = raw.decode("utf-8", errors="replace")[:_MAX_TEXT]
                if xml:
                    stripped = text.lstrip("﻿").lstrip()
                    if not (stripped.startswith("<?xml")
                            or stripped.startswith("<sitemapindex")
                            or stripped.startswith("<urlset")):
                        return empty
                return (text, final_url) if return_final_url else text
        except Exception:
            return empty


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def _parse_xml_safe(text: str) -> ET.Element | None:
    text = text.lstrip("﻿").lstrip("﻿")
    try:
        return ET.fromstring(text)
    except ET.ParseError:
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
    """Return (loc_url, lastmod) pairs. Uses is-None guards — never `or` on Elements."""
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
    all_urls = [s.get("url", "").lower() for s in sitemaps]
    all_fns  = [s.get("filename", "").lower() for s in sitemaps]
    root_lc  = found_url.lower()
    if any("sitemap_products_" in fn or "sitemap_collections_" in fn for fn in all_fns):
        return "shopify"
    if any("wp-sitemap" in u for u in all_urls) or "wp-sitemap" in root_lc:
        if any("product" in fn for fn in all_fns):
            return "woocommerce"
        return "wordpress"
    return ""


# ---------------------------------------------------------------------------
# SitemapReader  (verbatim logic from site_agent.SitemapReader)
# ---------------------------------------------------------------------------

class SitemapReader:
    """Isolated per-site sitemap reader.

    Each instance owns ALL crawl state (visited set, fetch budget, discovered
    sitemaps, found_url/type), so concurrent site reads never share mutable
    state and one site can never corrupt or stall another's sitemap pass.

    Returns a 7-tuple identical to site_agent.read_sitemap_async():
        (page_count, sitemap_url, sitemap_type, sitemaps,
         oldest_date, newest_date, platform)
    """

    def __init__(self, session: aiohttp.ClientSession, base_url: str,
                 *, debug: bool = False):
        self.session  = session
        self.base_url = base_url
        self.debug    = debug

    async def read(self) -> tuple[int, str, str, list[dict], str, str, str]:
        session  = self.session
        base_url = self.base_url
        debug    = self.debug

        _MAX_FETCHES      = 150
        _MAX_DEPTH        = 4
        _SAMPLE_PER_LEVEL = 30

        base    = base_url.rstrip("/")
        fetcher = _Fetcher(session)
        visited:      set[str]  = set()
        budget:       list[int] = [_MAX_FETCHES]
        found_url:    str       = ""
        found_type:   str       = "none"
        all_sitemaps: list[dict] = []

        # ── Discover from robots.txt ─────────────────────────────────────────
        robots_sitemaps: list[str] = []
        robots_text = await fetcher.get(f"{base}/robots.txt", timeout=10)
        for line in robots_text.splitlines():
            if line.strip().lower().startswith("sitemap:"):
                url = line.split(":", 1)[1].strip()
                if url and url not in robots_sitemaps:
                    robots_sitemaps.append(url)

        # Probe parent/grandparent dirs of deep robots.txt sitemap entries
        _INDEX_NAMES = ("sitemap_index.xml", "sitemap-index.xml", "sitemap.xml")
        extra_from_robots: list[str] = []
        for _sm_url in robots_sitemaps:
            try:
                _path        = urlparse(_sm_url).path
                _parent      = _path.rsplit("/", 1)[0]
                _grandparent = _parent.rsplit("/", 1)[0]
                for _dir in (_parent, _grandparent):
                    if _dir and _dir != "/":
                        for _name in _INDEX_NAMES:
                            _c = f"{base}{_dir}/{_name}"
                            if _c not in extra_from_robots and _c not in robots_sitemaps:
                                extra_from_robots.append(_c)
            except Exception:
                pass

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

            text, final_url = await fetcher.get(url, timeout=15, xml=True,
                                                 return_final_url=True)
            if final_url != url:
                if final_url in visited:
                    _dbg(f"{indent}REDIRECT-CYCLE  {url} -> {final_url}")
                    return 0
                visited.add(final_url)

            if not text:
                # HTML fallback: look for .xml hrefs or count page links
                html = await fetcher.get(url, timeout=15, xml=False)
                _dbg(f"{indent}HTML-fallback: html_len={len(html)} for {url}")
                if html:
                    child_urls = []
                    for m in re.finditer(
                        r"""href=["']((?:https?://[^"']*|/[^"']*)\.xml(?:\?[^"']*)?)["']""",
                        html, re.I
                    ):
                        child_url = m.group(1)
                        if not child_url.startswith("http"):
                            child_url = base + ("" if child_url.startswith("/") else "/") + child_url
                        if child_url not in visited and child_url not in child_urls:
                            child_urls.append(child_url)
                    _dbg(f"{indent}HTML-fallback: found {len(child_urls)} .xml hrefs")
                    if child_urls:
                        if not found_url:
                            found_url, found_type = url, "index"
                        sample_count = await _count_children([(u, "") for u in child_urls], depth)
                        all_sitemaps.append({"url": url, "filename": _sm_filename(url),
                                             "lastmod": "", "lastmod_newest": "",
                                             "page_count": sample_count})
                        return sample_count

                    # Yoast HTML urlset — count same-domain page links
                    _domain = urlparse(base).netloc
                    _skip_ext   = ('.xml', '.css', '.js', '.png', '.jpg', '.jpeg',
                                   '.gif', '.svg', '.ico', '.pdf', '.woff', '.woff2')
                    _skip_paths = ('#', 'mailto:', 'tel:', 'javascript:')
                    page_links: set[str] = set()
                    for m in re.finditer(
                        r'href=["\'](' + 'https?://' + re.escape(_domain) + r'/[^"\']*)["\']',
                        html, re.I
                    ):
                        href = m.group(1).split('#')[0].rstrip('/')
                        if (href
                                and not any(href.endswith(e) for e in _skip_ext)
                                and not any(p in href for p in _skip_paths)
                                and href != base):
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

                _dbg(f"{indent}EMPTY  {url}")
                return 0

            root = _parse_xml_safe(text)
            if root is None:
                # Non-XML content — try extracting child .xml hrefs
                child_urls = []
                for m in re.finditer(
                    r"""href=["']((?:https?://[^"']*|/[^"']*)\.xml(?:\?[^"']*)?)["']""",
                    text, re.I
                ):
                    child_url = m.group(1)
                    if not child_url.startswith("http"):
                        child_url = base + ("" if child_url.startswith("/") else "/") + child_url
                    if child_url not in visited and child_url not in child_urls:
                        child_urls.append(child_url)
                _dbg(f"{indent}HTML-from-xml-fetch: found {len(child_urls)} .xml hrefs")
                if child_urls:
                    if not found_url:
                        found_url, found_type = url, "index"
                    sample_count = await _count_children([(u, "") for u in child_urls], depth)
                    all_sitemaps.append({"url": url, "filename": _sm_filename(url),
                                         "lastmod": "", "lastmod_newest": "",
                                         "page_count": sample_count})
                    return sample_count
                return 0

            tag = root.tag.lower()
            _dbg(f"{indent}FETCH OK  tag={tag!r}  {url}")

            if "sitemapindex" in tag:
                if not found_url:
                    found_url, found_type = url, "index"
                entries  = _index_entries(root)
                children = [(u, lm) for u, lm in entries if u not in visited]
                _dbg(f"{indent}  index: {len(entries)} entries, {len(children)} unvisited")
                sample_count = await _count_children(children, depth)
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

        async def _count_children(children, depth: int) -> int:
            sample = list(children)[:_SAMPLE_PER_LEVEL]
            if not sample:
                return 0
            results = await asyncio.gather(
                *[_count_sitemap(u, depth + 1, parent_lastmod=lm) for (u, lm) in sample],
                return_exceptions=True,
            )
            nums    = [r for r in results if isinstance(r, int)]
            total   = sum(nums)
            sampled = len(sample)
            if len(children) > sampled and sampled > 0:
                total = int((total / sampled) * len(children))
            return total

        # ── Run all candidate entry points ───────────────────────────────────
        total = 0
        for candidate in candidates:
            total += await _count_sitemap(candidate)

        oldest_date = ""
        newest_date = ""
        for sm in all_sitemaps:
            lm = sm.get("lastmod", "")
            ln = sm.get("lastmod_newest", "")
            if lm and (not oldest_date or lm < oldest_date):
                oldest_date = lm
            if ln and (not newest_date or ln > newest_date):
                newest_date = ln

        platform = _detect_platform(found_url, all_sitemaps)

        return total, found_url, found_type, all_sitemaps, oldest_date, newest_date, platform
