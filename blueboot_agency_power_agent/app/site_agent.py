"""site_agent.py — Find content-heavy websites via Bing, measure size via sitemap,
and extract contacts from sites where a sitemap is found.

Pipeline per site:
  1. Bing search with queries from config/site_agent_queries.json
  2. Async fetch robots.txt → locate sitemap URL
  3. Async parse sitemap (index + urlset) → count pages
  4. If sitemap found: async crawl homepage + contact page → extract emails/phones
  5. Upsert SiteLead to Firestore `site_leads/{lead_id}`
  6. Upsert each contact to `site_leads/{lead_id}/site_contacts/{contact_id}`

All site fetching runs with asyncio + aiohttp, 20 tasks in parallel.
Bing queries run sequentially (sync) before the async phase.
No existing files are modified.

Usage:
    python app/site_agent.py --countries NO
    python app/site_agent.py --countries NO,SE,DK --max-results 50
    python app/site_agent.py --countries NO --min-pages 100 --dry-run
    python app/site_agent.py --countries ALL --workers 20

Parameters:
    --countries     Comma-separated ISO codes or ALL   (default: NO)
    --max-results   Max Bing results per query          (default: 50)
    --min-pages     Min sitemap pages to keep a site    (default: 0 = keep all)
    --workers       Async concurrency limit             (default: 20)
    --delay         Seconds between Bing queries        (default: 1.5)
    --no-firebase   Skip writing to Firestore
    --collection    Firestore collection                (default: site_leads)
    --dry-run       Print results, skip Firestore writes
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

import _pathsetup  # noqa: F401 — extends sys.path to project root + app/ + functions/ + collect-functions/

from functions.utils import (
    BROWSER_UA,
    company_from_domain,
    domain_of,
    extract_contacts,
    extract_phones,
    is_blocked,
    load_lines,
    normalize_url,
    pair_names_to_contacts,
    pair_phones_to_contacts,
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
    """Load domain blocklist patterns from the shared config file."""
    lines = load_lines(path)
    # The blocklist file has a CONTENT NEGATIVE KEYWORDS section — stop before it
    patterns: set[str] = set()
    for line in lines:
        if line.upper().startswith("CONTENT NEGATIVE"):
            break
        patterns.add(line.lower())
    return patterns


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class SiteContact:
    contact_id:   str   # SHA1 of normalised email
    email:        str
    name:         str
    title:        str
    phone:        str
    lead_id:      str
    domain:       str
    website:      str
    country:      str
    country_name: str
    found_on:     str   # URL where the email was found


@dataclass
class SiteLead:
    lead_id:       str
    domain:        str
    website:       str
    country:       str
    country_name:  str
    company:       str
    title:         str
    description:   str
    page_count:    int
    sitemap_url:   str
    sitemap_type:  str   # "index" | "urlset" | "none"
    source_query:  str
    crawled_at:    str
    contacts:      list[SiteContact] = field(default_factory=list, repr=False)


# lead_id_from_url is imported from functions.models — same logic as lead_agent


def _contact_id(email: str) -> str:
    return hashlib.sha1(email.lower().encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Bing search (sync — runs before the async phase)
# ---------------------------------------------------------------------------

def _bing_search(query: str, max_results: int,
                 exclude_domains: set[str] | None = None) -> list[str]:
    try:
        import search_runner   # on sys.path via _pathsetup → collect-functions/
        return search_runner.bing_search(query, max_results, exclude_domains)
    except Exception as exc:
        print(f"  [bing] error: {exc}")
        return []


# ---------------------------------------------------------------------------
# Async HTTP helpers
# ---------------------------------------------------------------------------

async def _async_get(session: aiohttp.ClientSession, url: str,
                     timeout: int = 15, xml: bool = False) -> str:
    """Fetch a URL; return text or '' on failure."""
    headers = dict(_HTTP_HEADERS)
    if xml:
        headers["Accept"] = "application/xml,text/xml,*/*;q=0.8"
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout),
                               allow_redirects=True, ssl=False) as resp:
            if resp.status != 200:
                return ""
            ct = resp.headers.get("content-type", "")
            # reject HTML pages returned for XML requests (e.g. 404 pages)
            if xml and "html" in ct:
                return ""
            return (await resp.text(errors="replace"))[:3_000_000]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Sitemap helpers (async)
# ---------------------------------------------------------------------------

_SM_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
_SITEMAP_PATHS = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/wp-sitemap.xml",
    "/sitemaps/sitemap.xml",
    "/sitemap/sitemap.xml",
]


def _parse_xml_safe(text: str) -> ET.Element | None:
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        # strip common XML declaration issues and retry
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
    """Return (page_count, sitemap_url, sitemap_type).
    sitemap_type: 'index' | 'urlset' | 'none'
    """
    base = base_url.rstrip("/")

    # --- robots.txt: look for Sitemap: directive ---
    robots_sitemap: str | None = None
    robots_text = await _async_get(session, f"{base}/robots.txt", timeout=10)
    for line in robots_text.splitlines():
        if line.strip().lower().startswith("sitemap:"):
            robots_sitemap = line.split(":", 1)[1].strip()
            break

    candidates = ([robots_sitemap] if robots_sitemap else []) + \
                 [base + p for p in _SITEMAP_PATHS]

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
            # sample up to 10 child sitemaps; extrapolate if more exist
            sample_count = 0
            sampled = 0
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
# Page meta (title + description) async
# ---------------------------------------------------------------------------

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_DESC_RE_A = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{0,300})',
    re.IGNORECASE,
)
_DESC_RE_B = re.compile(
    r'<meta[^>]+content=["\']([^"\']{0,300})["\'][^>]+name=["\']description["\']',
    re.IGNORECASE,
)


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
# Contact page discovery
# ---------------------------------------------------------------------------

_CONTACT_WORDS = [
    "contact", "kontakt", "kontakta", "about", "om-oss", "om oss",
    "team", "ansatte", "people", "staff", "company", "selskapet",
]
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)


def _find_contact_links(html: str, base_url: str, domain: str) -> list[str]:
    """Return internal links that look like contact/about pages (up to 3)."""
    links = []
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
    session:      aiohttp.ClientSession,
    website:      str,
    domain:       str,
    country:      str,
    country_name: str,
    lead_id:      str,
) -> list[SiteContact]:
    """Fetch homepage + up to 3 contact/about pages; extract emails + phones."""
    homepage_html = await _async_get(session, website, timeout=15)
    if not homepage_html:
        return []

    # Gather pages to scrape for contacts
    pages_to_scrape: list[str] = [website]
    contact_links = _find_contact_links(homepage_html, website, domain)
    pages_to_scrape.extend(contact_links)

    all_html = homepage_html
    page_html_map: dict[str, str] = {website: homepage_html}

    for link in contact_links:
        html = await _async_get(session, link, timeout=12)
        if html:
            all_html += "\n" + html
            page_html_map[link] = html

    # Extract contacts using existing utils
    combined_text = re.sub(r"<[^>]+>", " ", all_html)
    combined_text = re.sub(r"\s+", " ", combined_text)

    contacts: dict[str, str] = extract_contacts(all_html, combined_text)
    phones_set = extract_phones(combined_text, country)

    if not contacts:
        return []

    phone_region = country  # reuse country ISO for phonenumbers lib
    contact_phones = pair_phones_to_contacts(contacts, all_html + " " + combined_text, phone_region)
    contact_names  = pair_names_to_contacts(contacts, all_html + " " + combined_text, all_html)

    # Figure out which page each email was found on
    def _found_on(email: str) -> str:
        for page_url, page_html in page_html_map.items():
            if email.lower() in page_html.lower():
                return page_url
        return website

    result: list[SiteContact] = []
    for email in sorted(contacts.keys()):
        result.append(SiteContact(
            contact_id   = _contact_id(email),
            email        = email,
            name         = contact_names.get(email, ""),
            title        = contacts.get(email, ""),
            phone        = contact_phones.get(email, ""),
            lead_id      = lead_id,
            domain       = domain,
            website      = website,
            country      = country,
            country_name = country_name,
            found_on     = _found_on(email),
        ))
    return result


# ---------------------------------------------------------------------------
# Per-site async worker
# ---------------------------------------------------------------------------

async def process_site_async(
    session:      aiohttp.ClientSession,
    semaphore:    asyncio.Semaphore,
    url:          str,
    source_query: str,
    country:      str,
    country_name: str,
    min_pages:    int,
) -> SiteLead | None:
    """Full pipeline for one candidate URL — runs inside the semaphore."""
    async with semaphore:
        try:
            website = normalize_url(url)
            domain  = domain_of(website)
            if not domain:
                return None
        except Exception:
            return None

        lead_id = lead_id_from_url(website)

        # Step 1: sitemap
        page_count, sitemap_url, sitemap_type = await read_sitemap_async(session, website)

        if min_pages > 0 and page_count < min_pages:
            return None

        # Step 2: homepage meta
        homepage_html = await _async_get(session, website, timeout=15)
        title, description = _extract_meta(homepage_html) if homepage_html else ("", "")

        # Step 3: contacts (only when sitemap was found — site has real content)
        contacts: list[SiteContact] = []
        if sitemap_type != "none" and homepage_html:
            contacts = await scrape_contacts_async(
                session, website, domain, country, country_name, lead_id
            )

        return SiteLead(
            lead_id      = lead_id,
            domain       = domain,
            website      = website,
            country      = country,
            country_name = country_name,
            company      = company_from_domain(domain),
            title        = title,
            description  = description,
            page_count   = page_count,
            sitemap_url  = sitemap_url,
            sitemap_type = sitemap_type,
            source_query = source_query,
            crawled_at   = datetime.now(timezone.utc).isoformat(timespec="seconds"),
            contacts     = contacts,
        )


# ---------------------------------------------------------------------------
# Firestore
# ---------------------------------------------------------------------------

def _get_db(collection: str = COLLECTION_DEFAULT):
    try:
        from app.firestore_client import get_firestore
    except ImportError:
        from firestore_client import get_firestore
    db  = get_firestore()
    col = db.collection(collection)
    return db, col


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
        print(f"  [firebase] preload failed ({exc}) — starting fresh")
        return set()


def upsert_site_lead(lead: SiteLead, collection: str = COLLECTION_DEFAULT) -> None:
    """Write SiteLead + contacts to Firestore."""
    try:
        _, col = _get_db(collection)
        doc = asdict(lead)
        doc.pop("contacts", None)            # contacts live in subcollection
        col.document(lead.lead_id).set(doc, merge=True)

        if lead.contacts:
            contacts_col = col.document(lead.lead_id).collection("site_contacts")
            for contact in lead.contacts:
                contacts_col.document(contact.contact_id).set(asdict(contact), merge=True)
    except Exception as exc:
        print(f"  [firebase] upsert error for {lead.domain}: {exc}")


# ---------------------------------------------------------------------------
# Async orchestrator — Bing queries + site crawling run in the same event loop
# ---------------------------------------------------------------------------

BING_WORKERS = 5   # max concurrent Bing requests (be polite to Bing)


async def _bing_query_async(
    semaphore:    asyncio.Semaphore,
    query:        str,
    max_results:  int,
    delay:        float,
    seen_domains: set[str],
    blocklist:    set[str],
    counters:     dict,
) -> list[tuple[str, str]]:
    """Run one Bing query in a thread-pool executor (it's sync/blocking).
    Returns deduplicated (url, query) pairs that passed the blocklist check.

    asyncio is single-threaded so the seen_domains check+add is atomic —
    no other coroutine can interleave between the `if` and the `.add()`.
    """
    async with semaphore:
        loop = asyncio.get_event_loop()
        urls = await loop.run_in_executor(None, lambda: _bing_search(query, max_results))
        print(f"  [bing] {query!r}  → {len(urls)} results")
        await asyncio.sleep(delay)   # rate-limit per worker slot

    pairs: list[tuple[str, str]] = []
    for url in urls:
        d = domain_of(url)
        if not d:
            continue
        if d in seen_domains:
            counters["seen"] += 1
            continue
        if is_blocked(d, blocklist):
            counters["blocked"] += 1
            continue
        pairs.append((url, query))
        seen_domains.add(d)
    return pairs


async def _run_country_full_async(
    queries:      list[str],
    max_results:  int,
    delay:        float,
    blocklist:    set[str],
    seen_domains: set[str],
    country:      str,
    country_name: str,
    min_pages:    int,
    workers:      int,
    no_firebase:  bool,
    dry_run:      bool,
    collection:   str,
) -> list[SiteLead]:
    """Pipeline: Bing queries run in parallel and dispatch site tasks the moment
    results land — no waiting for all searches to finish before crawling starts.

    Timeline:
      [bing q1] ─→ results ─→ [site A] [site B] [site C] ...
      [bing q2] ─→ results ─→ [site D] [site E] ...      (q2 finishes later)
      [bing q3] ─→ results ─→ [site F] ...               (q3 still running)
      ...all site tasks limited to `workers` concurrent via site_sem...
    """
    bing_sem   = asyncio.Semaphore(BING_WORKERS)
    site_sem   = asyncio.Semaphore(workers)
    counters   = {"seen": 0, "blocked": 0, "done": 0}
    site_tasks: list[asyncio.Task] = []

    connector = aiohttp.TCPConnector(limit=workers, limit_per_host=3, ssl=False)
    timeout   = aiohttp.ClientTimeout(total=30, connect=8)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:

        async def bing_and_dispatch(query: str) -> None:
            """Run one Bing query, then immediately create a site task per result."""
            pairs = await _bing_query_async(
                bing_sem, query, max_results, delay,
                seen_domains, blocklist, counters,
            )
            for url, q in pairs:
                task = asyncio.create_task(
                    process_site_async(session, site_sem, url, q,
                                       country, country_name, min_pages)
                )
                site_tasks.append(task)

        # Fire all Bing queries concurrently; each one dispatches site tasks
        # as soon as it gets results — site crawling starts before Bing is done.
        await asyncio.gather(*[bing_and_dispatch(q) for q in queries])

        print(f"\n  [site_agent] skipped {counters['seen']} already-stored, "
              f"{counters['blocked']} blocklisted")
        print(f"  [site_agent] {len(site_tasks)} sites queued "
              f"(crawling already in progress)\n")

        # Collect results — many tasks are already running or finished by now.
        leads: list[SiteLead] = []
        for coro in asyncio.as_completed(site_tasks):
            lead = await coro
            counters["done"] += 1
            n = counters["done"]
            total = len(site_tasks)
            if lead:
                leads.append(lead)
                contact_info = f"  {len(lead.contacts)} contacts" if lead.contacts else ""
                sitemap_info = f"pages={lead.page_count} ({lead.sitemap_type})"
                print(f"  [{n}/{total}] ✓ {lead.domain:<40} {sitemap_info}{contact_info}")
                if not no_firebase and not dry_run:
                    upsert_site_lead(lead, collection)
            else:
                print(f"  [{n}/{total}] – (filtered/failed)")

    return leads


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run(
    countries:   list[str],
    max_results: int   = 50,
    min_pages:   int   = 0,
    workers:     int   = WORKERS_DEFAULT,
    delay:       float = 1.5,
    no_firebase: bool  = False,
    collection:  str   = COLLECTION_DEFAULT,
    dry_run:     bool  = False,
) -> list[SiteLead]:
    config   = load_site_config()
    blocklist = load_blocklist()
    print(f"  [blocklist] {len(blocklist)} patterns loaded")

    # Always preload seen domains so already-stored sites are skipped,
    # even on --dry-run (avoids duplicate work when iterating).
    seen_domains: set[str] = set()
    if not no_firebase:
        seen_domains = preload_seen_domains(collection)

    all_leads: list[SiteLead] = []

    for country_code in countries:
        country_code = country_code.upper()
        cfg = config.get(country_code)
        if not cfg:
            print(f"  [site_agent] No config for '{country_code}' — skipping.")
            continue

        country_name  = cfg.get("name", country_code)
        queries       = cfg.get("queries", [])
        country_min   = cfg.get("min_pages", 0)
        effective_min = max(min_pages, country_min)

        print(f"\n{'='*60}")
        print(f"  Country  : {country_name} ({country_code})")
        print(f"  Queries  : {len(queries)} (running {min(BING_WORKERS, len(queries))} in parallel)")
        print(f"  Min pages: {effective_min}")
        print(f"  Workers  : {workers}")
        print(f"{'='*60}")

        country_leads = asyncio.run(
            _run_country_full_async(
                queries      = queries,
                max_results  = max_results,
                delay        = delay,
                blocklist    = blocklist,
                seen_domains = seen_domains,
                country      = country_code,
                country_name = country_name,
                min_pages    = effective_min,
                workers      = workers,
                no_firebase  = no_firebase,
                dry_run      = dry_run,
                collection   = collection,
            )
        )

        all_leads.extend(country_leads)

        contacts_total = sum(len(l.contacts) for l in country_leads)
        print(f"\n  [site_agent] {country_code} done — "
              f"{len(country_leads)} sites, {contacts_total} contacts stored.")

    print(f"\n{'='*60}")
    total_contacts = sum(len(l.contacts) for l in all_leads)
    print(f"  TOTAL: {len(all_leads)} sites | {total_contacts} contacts "
          f"across {len(countries)} country(-ies)")
    print(f"{'='*60}\n")
    return all_leads


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> None:
    from dotenv import load_dotenv
    load_dotenv()

    p = argparse.ArgumentParser(
        description="Site Agent — find content-heavy websites via Bing + sitemap + contact extraction"
    )
    p.add_argument("--countries",    default="NO",
                   help="Comma-separated ISO codes or ALL  (default: NO)")
    p.add_argument("--max-results",  type=int, default=50, metavar="N",
                   help="Max Bing results per query  (default: 50)")
    p.add_argument("--min-pages",    type=int, default=0, metavar="N",
                   help="Minimum sitemap page count to keep a site  (default: 0)")
    p.add_argument("--workers",      type=int, default=WORKERS_DEFAULT, metavar="N",
                   help=f"Async concurrency limit  (default: {WORKERS_DEFAULT})")
    p.add_argument("--delay",        type=float, default=1.5, metavar="SECS",
                   help="Seconds between Bing queries  (default: 1.5)")
    p.add_argument("--no-firebase",  action="store_true",
                   help="Skip writing to Firestore")
    p.add_argument("--collection",   default=COLLECTION_DEFAULT, metavar="NAME",
                   help=f"Firestore collection  (default: {COLLECTION_DEFAULT})")
    p.add_argument("--dry-run",      action="store_true",
                   help="Print results without writing to Firestore")

    args = p.parse_args(argv)

    config = load_site_config()
    if args.countries.upper() == "ALL":
        countries = [k for k in config if not k.startswith("_")]
    else:
        countries = [c.strip().upper() for c in args.countries.split(",") if c.strip()]

    run(
        countries   = countries,
        max_results = args.max_results,
        min_pages   = args.min_pages,
        workers     = args.workers,
        delay       = args.delay,
        no_firebase = args.no_firebase,
        collection  = args.collection,
        dry_run     = args.dry_run,
    )


if __name__ == "__main__":
    main()
