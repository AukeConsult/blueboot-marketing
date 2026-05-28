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
    target_types:  list[str] = field(default_factory=list)
    keywords:      list[str] = field(default_factory=list)
    contacts:      list[SiteContact] = field(default_factory=list, repr=False)


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


# ---------------------------------------------------------------------------
# Async HTTP helpers
# ---------------------------------------------------------------------------

async def _async_get(session: aiohttp.ClientSession, url: str,
                     timeout: int = 15, xml: bool = False) -> str:
    headers = dict(_HTTP_HEADERS)
    if xml:
        headers["Accept"] = "application/xml,text/xml,*/*;q=0.8"
    try:
        async with session.get(
            url, headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=True, ssl=False,
        ) as resp:
            if resp.status != 200:
                return ""
            ct = resp.headers.get("content-type", "")
            if xml and "html" in ct:
                return ""
            return (await resp.text(errors="replace"))[:3_000_000]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Sitemap helpers
# ---------------------------------------------------------------------------

_SM_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
_SITEMAP_PATHS = [
    "/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
    "/wp-sitemap.xml", "/sitemaps/sitemap.xml", "/sitemap/sitemap.xml",
]


def _parse_xml_safe(text: str) -> ET.Element | None:
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        cleaned = re.sub(r"<\?xml[^?]*\?>", "", text, count=1).strip()
        try:
            return ET.fromstring(cleaned)
        except ET.ParseError:
            return None


def _count_urls(root: ET.Element) -> int:
    n = len(root.findall(f"{{{_SM_NS}}}url"))
    return n or len(root.findall("url"))


def _index_children(root: ET.Element) -> list[str]:
    locs = root.findall(f"{{{_SM_NS}}}sitemap/{{{_SM_NS}}}loc")
    if not locs:
        locs = root.findall("sitemap/loc")
    return [loc.text.strip() for loc in locs if loc.text]


async def read_sitemap_async(session: aiohttp.ClientSession,
                             base_url: str) -> tuple[int, str, str]:
    base = base_url.rstrip("/")
    robots_sitemap: str | None = None
    robots_text = await _async_get(session, f"{base}/robots.txt", timeout=10)
    for line in robots_text.splitlines():
        if line.strip().lower().startswith("sitemap:"):
            robots_sitemap = line.split(":", 1)[1].strip()
            break

    candidates = ([robots_sitemap] if robots_sitemap else []) + [base + p for p in _SITEMAP_PATHS]
    for sitemap_url in candidates:
        text = await _async_get(session, sitemap_url, timeout=15, xml=True)
        if not text:
            continue
        root = _parse_xml_safe(text)
        if root is None:
            continue
        tag = root.tag.lower()
        if "sitemapindex" in tag:
            children = _index_children(root)
            sample_count = sampled = 0
            for child_url in children[:10]:
                child_text = await _async_get(session, child_url, timeout=12, xml=True)
                if child_text:
                    child_root = _parse_xml_safe(child_text)
                    if child_root is not None:
                        sample_count += _count_urls(child_root)
                        sampled += 1
            if sampled == 0:
                total = 0
            elif len(children) > sampled:
                total = int((sample_count / sampled) * len(children))
            else:
                total = sample_count
            return total, sitemap_url, "index"
        if "urlset" in tag:
            return _count_urls(root), sitemap_url, "urlset"
    return 0, "", "none"


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
    target_types: list[str] | None = None,
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
    page_count, sitemap_url, sitemap_type = await read_sitemap_async(session, website)
    if min_pages > 0 and page_count < min_pages:
        print(f"    skip {domain}  pages={page_count} ({sitemap_type}) < min={min_pages}")
        return None, f"min_pages:{page_count}"

    homepage_html = await _async_get(session, website, timeout=15)
    title, description = _extract_meta(homepage_html) if homepage_html else ("", "")

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
    domain:       str,
    website:      str,
    lead_id:      str,
    country:      str,
    reason:       str,
    page_count:   int   = 0,
    source_query: str   = "",
    collection:   str   = EXCLUDED_COLLECTION_DEFAULT,
) -> None:
    """Record a rejected site so it is skipped on future runs."""
    try:
        _, col = _get_db(collection)
        col.document(lead_id).set({
            "lead_id":      lead_id,
            "domain":       domain,
            "website":      website,
            "country":      country,
            "reason":       reason,
            "page_count":   page_count,
            "source_query": source_query,
            "excluded_at":  datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
) -> None:
    async with semaphore:
        loop = asyncio.get_running_loop()
        try:
            urls = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: _bing_search(query, max_results)),
                timeout=45.0,
            )
        except asyncio.TimeoutError:
            print(f"  [bing] timeout: {query!r}")
            urls = []
        print(f"  [bing] {query!r}  -> {len(urls)} results")
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
        await queue.put((url, query, effective_country))


async def _run_country_full_async(
    queries:          list[str],
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

    connector = aiohttp.TCPConnector(limit=workers, limit_per_host=3, ssl=False)
    timeout   = aiohttp.ClientTimeout(total=30, connect=8)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:

        async def site_consumer() -> None:
            while True:
                item = await queue.get()
                try:
                    if item is _QUEUE_SENTINEL:
                        break
                    url, source_query, eff_country = item
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
                        lead, excl_reason = await process_site_async(
                            session, url, source_query,
                            eff_country, eff_country_name,
                            min_pages, target_types,
                        )
                    except Exception as exc:
                        counters["done"] += 1
                        n = counters["done"]
                        excl_reason = "error"
                        print(f"  [{n}] [consumer] error on {url}: {exc}")
                        try:
                            if not no_firebase and not dry_run:
                                lead_id = lead_id_from_url(normalize_url(url))
                                _args = (d, url, lead_id, eff_country,
                                         excl_reason, 0, source_query, excl_collection)
                                await asyncio.wait_for(
                                    loop.run_in_executor(
                                        None, lambda a=_args: upsert_site_excluded(*a)
                                    ),
                                    timeout=12.0,
                                )
                            excluded_domains.add(d)
                            counters["excluded"] += 1
                        except Exception:
                            pass
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
                                print(f"  [{n}] [lead-write] timeout/error: {exc}")
                    else:
                        # Parse out real page_count if encoded in the reason string
                        stored_reason = excl_reason
                        stored_pages  = 0
                        if excl_reason.startswith("min_pages:"):
                            stored_pages  = int(excl_reason.split(":", 1)[1] or 0)
                            stored_reason = "min_pages"
                        print(f"  [{n}] -- excluded ({stored_reason})  {url}")
                        if excl_reason != "url_error":
                            try:
                                if not no_firebase and not dry_run:
                                    lead_id = lead_id_from_url(normalize_url(url))
                                    _args = (d, url, lead_id, eff_country,
                                             stored_reason, stored_pages,
                                             source_query, excl_collection)
                                    await asyncio.wait_for(
                                        loop.run_in_executor(
                                            None, lambda a=_args: upsert_site_excluded(*a)
                                        ),
                                        timeout=12.0,
                                    )
                                excluded_domains.add(d)
                                counters["excluded"] += 1
                            except (asyncio.TimeoutError, Exception) as exc:
                                print(f"  [{n}] [excl-upsert] timeout/error: {exc}")

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
            )
            for q in queries
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
        query_cats = cfg.get("query_categories")
        if query_cats:
            if category:
                cat_lower = category.lower()
                if cat_lower not in query_cats:
                    available = ", ".join(query_cats.keys())
                    print(f"  [site_agent] Category '{cat_lower}' not found for {country_code}. "
                          f"Available: {available}")
                    continue
                queries     = query_cats[cat_lower]
                cat_display = cat_lower
            else:
                queries     = [q for qs in query_cats.values() for q in qs]
                cat_display = "ALL (" + ", ".join(query_cats.keys()) + ")"
        else:
            queries     = cfg.get("queries", [])
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