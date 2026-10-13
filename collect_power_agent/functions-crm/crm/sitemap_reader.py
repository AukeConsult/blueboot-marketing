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

_UA = "BlueBootLeadAgent/1.1 (+https://blueboot.ai)"

# WordPress / Yoast SEO serves raw XML only to crawlers (Googlebot-style UA).
# With a browser UA they serve an HTML/XSLT view that fails our XML content check.
# Municipal sites that block Googlebot fall back to the HTML-link path automatically.
_BOT_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

_HTTP_HEADERS = {
    "User-Agent":      _UA,
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_XML_HEADERS = {
    **_HTTP_HEADERS,
    "User-Agent": _BOT_UA,   # Googlebot UA for sitemap XML — needed for WordPress/Yoast raw XML
}

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

    def __init__(self, session: aiohttp.ClientSession, *, debug: bool = False):
        self._session = session
        self._debug   = debug

    async def get(self, url: str, *, timeout: float = 15.0, xml: bool = False,
                  return_final_url: bool = False):
        headers = dict(_XML_HEADERS if xml else _HTTP_HEADERS)
        if xml:
            headers.setdefault("Accept", "application/xml,text/xml,*/*;q=0.8")
        empty = ("", url) if return_final_url else ""
        dbg   = self._debug
        try:
            async with self._session.get(
                url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=True, ssl=False,
            ) as resp:
                final_url = str(resp.url)
                ct = resp.headers.get("Content-Type", "")
                ce = resp.headers.get("Content-Encoding", "")
                if dbg and xml:
                    print(f"    [fetch-dbg] {resp.status} ct={ct!r} ce={ce!r}  {url}")
                if resp.status != 200:
                    if dbg:
                        print(f"    [fetch-dbg] SKIP non-200 ({resp.status})  {url}")
                    return empty
                # Read the complete document in chunks — ensures we never get a
                # partial body even on chunked transfer-encoding or slow servers.
                _chunks: list[bytes] = []
                _read = 0
                async for _chunk in resp.content.iter_chunked(65536):
                    _chunks.append(_chunk)
                    _read += len(_chunk)
                    if _read > _MAX_BODY:
                        break
                raw = b"".join(_chunks)
                if len(raw) > _MAX_BODY:
                    raw = raw[:_MAX_BODY]
                gzipped = raw[:2] == b"\x1f\x8b"
                if dbg and xml:
                    print(f"    [fetch-dbg] len={len(raw)} gzip={gzipped} hex={raw[:8].hex()}  {url}")
                if gzipped:
                    try:
                        with _gzip.GzipFile(fileobj=_io.BytesIO(raw)) as gz:
                            raw = gz.read(_MAX_BODY)
                        if dbg:
                            print(f"    [fetch-dbg] after gunzip len={len(raw)} hex={raw[:8].hex()}")
                    except Exception as _gz_exc:
                        if dbg:
                            print(f"    [fetch-dbg] gunzip FAILED: {_gz_exc}")
                        return empty
                text = raw.decode("utf-8", errors="replace")[:_MAX_TEXT]
                if xml:
                    stripped = text.lstrip("\ufeff").lstrip()
                    ok = (stripped.startswith("<?xml")
                          or stripped.startswith("<sitemapindex")
                          or stripped.startswith("<urlset"))
                    if dbg:
                        print(f"    [fetch-dbg] xml_check={'PASS' if ok else 'FAIL'} peek={stripped[:60]!r}")
                    if not ok:
                        return empty
                return (text, final_url) if return_final_url else text
        except Exception as _exc:
            if dbg:
                print(f"    [fetch-dbg] EXCEPTION {type(_exc).__name__}: {_exc}  {url}")
            return empty


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def _fix_entities(text: str) -> str:
    """Escape bare & that are not already part of a valid XML entity reference.

    Norwegian municipal CMS systems (eKommune/ePublish) often generate sitemaps
    with unescaped & in query-string URLs inside <loc> tags, e.g.:
        <loc>https://example.no/page?a=1&b=2</loc>
    ElementTree rejects this as undefined entity.  We fix it before parsing.
    """
    return re.sub(r'&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)', '&amp;', text)


def _parse_xml_safe(text: str, debug: bool = False) -> ET.Element | None:
    text = text.lstrip("﻿").lstrip()
    text = _fix_entities(text)
    try:
        return ET.fromstring(text)
    except ET.ParseError as _e1:
        # Strip processing instructions (<?xml...?>, <?xml-stylesheet...?>) and retry.
        cleaned = re.sub(r"<\?[^>]*?\?>", "", text).strip()
        cleaned = _fix_entities(cleaned)
        try:
            return ET.fromstring(cleaned)
        except ET.ParseError as _e2:
            if debug:
                print(f"    [parse-dbg] PARSE-FAIL e1={_e1!r} e2={_e2!r}")
                print(f"    [parse-dbg]   text_len={len(text)}  head={text[:80]!r}")
                print(f"    [parse-dbg]   tail={text[-80:]!r}")
            return None


def _count_urls(root: ET.Element) -> int:
    n = len(root.findall(f"{{{_SM_NS}}}url"))
    return n or len(root.findall("url"))


def _count_urls_regex(text: str) -> int:
    """Count <url> tags with regex — handles truncated/malformed XML."""
    return len(re.findall(r"<url[\s>]", text, re.IGNORECASE))


def _index_entries_regex(text: str) -> list[tuple[str, str]]:
    """Extract (loc, lastmod) from <sitemap>…</sitemap> blocks using regex."""
    results = []
    for block in re.finditer(r"<sitemap[\s>]([\s\S]*?)</sitemap>", text, re.IGNORECASE):
        inner = block.group(1)
        loc_m = re.search(r"<loc[^>]*>\s*([\s\S]*?)\s*</loc>",     inner, re.IGNORECASE)
        lm_m  = re.search(r"<lastmod[^>]*>\s*([\s\S]*?)\s*</lastmod>", inner, re.IGNORECASE)
        loc   = loc_m.group(1).strip() if loc_m else ""
        lm    = lm_m.group(1).strip()  if lm_m  else ""
        if loc:
            results.append((loc, lm))
    return results


def _urlset_lastmods_regex(text: str) -> list[str]:
    """Return all <lastmod> date strings found in a urlset via regex."""
    dates = []
    for m in re.finditer(r"<lastmod[^>]*>\s*([\s\S]*?)\s*</lastmod>", text, re.IGNORECASE):
        d = m.group(1).strip()
        if d:
            dates.append(d)
    return dates


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
        fetcher = _Fetcher(session, debug=debug)
        visited:      set[str]  = set()
        budget:       list[int] = [_MAX_FETCHES]
        found_url:    str       = ""
        found_type:   str       = "none"
        all_sitemaps: list[dict] = []

        # ── Discover from robots.txt ─────────────────────────────────────────
        robots_sitemaps: list[str] = []
        robots_text = await fetcher.get(f"{base}/robots.txt", timeout=10)
        if debug:
            print(f"    [sitemap-dbg] robots.txt: {len(robots_text)} chars")
        for line in robots_text.splitlines():
            if line.strip().lower().startswith("sitemap:"):
                url = line.split(":", 1)[1].strip()
                if url and url not in robots_sitemaps:
                    robots_sitemaps.append(url)
        if debug:
            print(f"    [sitemap-dbg] robots.txt sitemaps: {robots_sitemaps or '(none)'}")

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

        if debug:
            print(f"    [sitemap-dbg] {len(candidates)} candidates to try:")
            for _c in candidates[:10]:
                print(f"    [sitemap-dbg]   {_c}")
            if len(candidates) > 10:
                print(f"    [sitemap-dbg]   ... and {len(candidates)-10} more")

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

            # ── Regex-based processing (mirrors TypeScript sitemap-scanner.ts) ──
            # Works on truncated/malformed XML without a parser.
            stripped  = text.lstrip("\ufeff").lstrip()
            is_index  = "<sitemapindex" in stripped
            is_urlset = not is_index and "<urlset" in stripped

            if is_index:
                if not found_url:
                    found_url, found_type = url, "index"
                entries  = _index_entries_regex(text)
                children = [(u, lm) for u, lm in entries if u not in visited]
                _dbg(f"{indent}index: {len(entries)} entries, {len(children)} unvisited  {url}")
                sample_count = await _count_children(children, depth)
                all_sitemaps.append({"url": url, "filename": _sm_filename(url),
                                     "lastmod": parent_lastmod, "lastmod_newest": parent_lastmod,
                                     "page_count": sample_count})
                _dbg(f"{indent}index total={sample_count:,}")
                return sample_count

            if is_urlset:
                count    = _count_urls_regex(text)
                lm_dates = _urlset_lastmods_regex(text)
                oldest   = (min(lm_dates) if lm_dates else "") or parent_lastmod
                newest   = (max(lm_dates) if lm_dates else "") or parent_lastmod
                all_sitemaps.append({"url": url, "filename": _sm_filename(url),
                                     "lastmod": oldest, "lastmod_newest": newest,
                                     "page_count": count})
                if not found_url:
                    found_url, found_type = url, "urlset"
                _dbg(f"{indent}urlset  count={count:,}  {url}")
                return count

            # Not recognised XML — try extracting child .xml hrefs from HTML response
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
            _dbg(f"{indent}UNKNOWN content  {url}")
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
