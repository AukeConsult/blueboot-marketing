"""Search mode — Bing/Google search, site crawling, batch orchestration, run()."""
from __future__ import annotations

import asyncio
import re
import base64
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote

import aiohttp
import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from app.functions.utils import (
    USER_AGENT, BROWSER_UA,
    normalize_url, domain_of, company_from_domain,
    is_blocked, country_for_domain, tld_accepted_for,
    fetch, extract_meta, visible_text, extract_contacts, extract_phones,
    pair_phones_to_contacts, pair_names_to_contacts, extract_links, detect_tech, categorize, priority, angle,
    load_lines, load_country_configs, selected_countries, DEFAULT_COUNTRIES,
    linkedin_hints, normalize_phone_list,
)
from app.functions.firebase_sync import upsert_lead, upsert_lead_excluded, load_leads_excluded
from app.functions.models import Lead, dedupe_leads, export


# ---------------------------------------------------------------------------
# Search backends
# ---------------------------------------------------------------------------

def bing_search(query: str, max_results: int,
                exclude_domains: set[str] | None = None) -> list[str]:
    """Search Bing via RSS feed."""
    import xml.etree.ElementTree as ET
    # Prefix every term with + so Bing requires ALL words to appear in each result.
    # e.g. "webbyrå ålesund" → "+webbyrå +ålesund"
    # Quoted phrases and existing +/- operators are left untouched.
    def _require_all(raw: str) -> str:
        tokens = []
        for word in raw.split():
            if word.startswith(("+", "-", '"')):
                tokens.append(word)   # already has an operator — leave it
            else:
                tokens.append("+" + word)
        return " ".join(tokens)

    q = _require_all(query)
    if exclude_domains:
        q += " " + " ".join(f"-site:{d}" for d in list(exclude_domains)[:20])
    # Bing RSS returns ~10 results per page; paginate with first=1,11,21,...
    PAGE_SIZE = 10
    urls, seen, page = [], set(), 1
    while len(urls) < max_results:
        first = (page - 1) * PAGE_SIZE + 1
        try:
            resp = requests.get(
                "https://www.bing.com/search",
                params={"q": q, "format": "rss", "count": PAGE_SIZE, "first": first},
                headers={"User-Agent": BROWSER_UA, "Accept": "application/rss+xml,*/*",
                         "Accept-Language": "en-US,en;q=0.9"},
                timeout=20,
            )
            root = ET.fromstring(resp.text)
        except Exception as e:
            print(f"  [Bing] error: {e}")
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
        if not added:
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
        try:
            r = requests.get(
                "https://www.googleapis.com/customsearch/v1",
                params={"key": key, "cx": cse, "q": query,
                        "num": min(10, max_results - len(urls)), "start": start},
                timeout=20,
            )
            r.raise_for_status()
            urls += [item["link"] for item in r.json().get("items", []) if item.get("link")]
        except Exception:
            break
        if len(urls) >= max_results:
            break
    return urls[:max_results]


def brave_search(query: str, max_results: int,
                country_code: str = "") -> list[str]:
    """Search via Brave Search API.

    Requires BRAVE_API_KEY env var.
    Free tier: 2,000 requests/month — https://api.search.brave.com/
    Paid tiers available for higher volume.
    country_code: ISO 2-letter code (e.g. "NO", "SE") — passed as Brave country + search_lang.
    Returns up to max_results URL strings.
    """
    api_key = cfg.BRAVE_API_KEY
    if not api_key:
        return []

    # Brave expects lowercase 2-letter country code, e.g. "no", "se", "dk"
    # Map internal country codes to ISO 3166-1 alpha-2 for Brave API
    _BRAVE_CC_MAP = {"uk": "gb", "en": "gb", "qq": ""}  # QQ = global, no country filter
    cc = country_code.lower() if country_code else ""
    cc = _BRAVE_CC_MAP.get(cc, cc)

    urls: list[str] = []
    # Brave free tier: single request only, max 20 results per call
    params: dict = {
        "q":          query,
        "count":      min(20, max_results),
        "safesearch": "off",
    }
    if cc:
        params["country"] = cc   # Brave uses ISO 3166-1 alpha-2 lowercase, e.g. "no", "se"

    try:
        resp = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            params=params,
            headers={
                "Accept":               "application/json",
                "Accept-Encoding":      "gzip",
                "X-Subscription-Token": api_key,
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [Brave] error: {e}")
        return []

    for item in data.get("web", {}).get("results", []):
        u = item.get("url", "").strip()
        if u and u not in urls:
            urls.append(u)

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


# ---------------------------------------------------------------------------
# GitHub organisation search
# ---------------------------------------------------------------------------

def github_org_search(country_cfg: dict, country_code: str,
                      max_orgs: int = 200) -> list[str]:
    """Search GitHub for web-agency organisations in a country.

    Returns a list of website URLs extracted from the org's GitHub profile.
    Requires GITHUB_TOKEN env var for higher rate limits (recommended).
    Without a token the API allows only 10 req/min — it will still work but
    will be slow and may hit limits for large runs.
    """
    import time

    token   = cfg.GITHUB_TOKEN
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    country_name = country_cfg.get("name", country_code)
    # GitHub location search works best with English terms — always include
    # "web agency" as anchor, then add one native keyword if available.
    agency_kw   = country_cfg.get("keywords", {}).get("web_agency", [])
    native_term = agency_kw[0] if agency_kw else ""
    # Avoid duplicating "web agency" if the native term is the same
    if native_term.lower() in ("web agency", ""):
        terms = "web agency digital agency website"
    else:
        terms = f"web agency {native_term}"
    query = f"type:org location:{country_name} {terms}"

    websites: list[str] = []
    page = 1
    per_page = 100
    seen_logins: set[str] = set()

    print(f"  [GitHub] Searching orgs: {query!r}")

    while len(websites) < max_orgs:
        try:
            r = requests.get(
                "https://api.github.com/search/users",
                params={"q": query, "per_page": per_page, "page": page},
                headers=headers,
                timeout=20,
            )
            if r.status_code == 403:
                reset = int(r.headers.get("X-RateLimit-Reset", 0))
                wait  = max(reset - int(time.time()), 1)
                print(f"  [GitHub] rate-limited — waiting {wait}s")
                time.sleep(min(wait, 60))
                continue
            if r.status_code == 422:
                # Unprocessable — query too complex or no results
                break
            r.raise_for_status()
            data  = r.json()
            items = data.get("items", [])
            if not items:
                break
        except Exception as exc:
            print(f"  [GitHub] search error: {exc}")
            break

        logins = [it["login"] for it in items if it["login"] not in seen_logins]
        seen_logins.update(logins)

        # Fetch each org profile to get their website
        for login in logins:
            if len(websites) >= max_orgs:
                break
            try:
                pr = requests.get(
                    f"https://api.github.com/users/{login}",
                    headers=headers,
                    timeout=10,
                )
                pr.raise_for_status()
                blog = (pr.json().get("blog") or "").strip()
                if blog and blog.startswith("http") and "github" not in blog.lower():
                    websites.append(blog)
                    print(f"  [GitHub] {login} -> {blog}")
                # Be polite — GitHub allows 30 authenticated req/min
                time.sleep(0.5 if token else 2.0)
            except Exception:
                pass

        if len(items) < per_page:
            break  # last page
        page += 1

    print(f"  [GitHub] found {len(websites)} org websites for {country_code}")
    return websites


# ---------------------------------------------------------------------------
# Site crawlers
# ---------------------------------------------------------------------------

def crawl_site(url: str, source_query: str, max_pages: int, delay: float,
               country_code: str, country_cfg: dict) -> Lead | None:
    """Synchronous single-site crawler."""
    import time
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
        all_html += " " + html[:300_000]
        contacts.update(extract_contacts(html, text))
        phones |= extract_phones(text, country_cfg.get("phone_region", country_code))
        tech   |= detect_tech(html, soup)
        links, cp, li = extract_links(page, soup)
        contact_page = contact_page or cp
        linkedin     = linkedin or li
        for lnk in links:
            low = lnk.lower().split("#")[0]
            if domain_of(low) == dom and any(
                x in low for x in country_cfg.get("contact_words", ["contact","about","services","case"])
            ):
                if low not in seen and low not in queue:
                    queue.append(low)
        time.sleep(delay)

    if not all_text and not all_html:
        return None
    cats, reasons, score = categorize(all_text, all_html, country_cfg)
    if score < 20 and fuzz.partial_ratio(
        "digitalbyraa webdesign wordpress seo", all_text[:5000].lower()
    ) < 35:
        return None
    lead_angle = angle(cats, tech)
    sorted_emails  = sorted(contacts.keys())
    phone_region   = country_cfg.get("phone_region", country_code)
    combined       = all_html + " " + all_text
    contact_phones = pair_phones_to_contacts(contacts, combined, phone_region)
    contact_names  = pair_names_to_contacts(contacts, combined, all_html)
    return Lead(
        company=company_from_domain(dom), domain=dom, website=website,
        source_query=source_query, title=title, description=desc,
        emails=", ".join(sorted_emails),
        email_titles=", ".join(contacts.get(e, "") for e in sorted_emails),
        email_phones=normalize_phone_list(", ".join(contact_phones.get(e, "") for e in sorted_emails)),
        email_names=", ".join(contact_names.get(e, "") for e in sorted_emails),
        phones=normalize_phone_list(", ".join(sorted(phones))), contact_page=contact_page,
        linkedin=linkedin or linkedin_hints.get(website, ""),
        detected_tech=", ".join(sorted(tech)),
        categories=", ".join(sorted(cats)),
        reseller_score=score, priority=priority(score), reasons="; ".join(reasons),
        suggested_angle=lead_angle,
        country=country_code, country_name=country_cfg.get("name", country_code),
        crawled_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


async def _async_fetch(session: aiohttp.ClientSession, url: str,
                       accept_language: str = "en;q=0.8") -> str:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": accept_language}
    async with session.get(url, headers=headers, allow_redirects=True) as resp:
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        if "text" not in ct and "html" not in ct:
            return ""
        return (await resp.text(errors="replace"))[:2_000_000]


async def _async_crawl_site(
    session: aiohttp.ClientSession,
    url: str, source_query: str, max_pages: int, delay: float,
    country_code: str, country_cfg: dict,
    min_score: int = 50,
    source: str = "search",     # "search" or "catalog" — recorded on the Lead
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
            html = await _async_fetch(session, page, country_cfg.get("accept_language", "en;q=0.8"))
        except Exception:
            continue
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        if not title:
            title, desc = extract_meta(soup)
        text = visible_text(soup)
        all_text += " " + text
        all_html += " " + html[:300_000]
        contacts.update(extract_contacts(html, text))
        phones |= extract_phones(text, country_cfg.get("phone_region", country_code))
        tech   |= detect_tech(html, soup)
        links, cp, li = extract_links(page, soup)
        contact_page = contact_page or cp
        linkedin     = linkedin or li
        for lnk in links:
            low = lnk.lower().split("#")[0]
            if domain_of(low) == dom and any(
                x in low for x in country_cfg.get("contact_words", ["contact","about","services","case"])
            ):
                if low not in seen and low not in queue:
                    queue.append(low)
        await asyncio.sleep(delay)

        # --- Early exit after page 1: only drop sites with zero agency signal ---
        # We use 0 (not min_score/2) so sites that score low on the homepage
        # but reveal their agency nature on services/contact pages are not missed.
        if len(seen) == 1:
            _, _, quick_score = categorize(all_text, all_html, country_cfg)
            if quick_score == 0:
                return None

    if not all_text and not all_html:
        return None
    cats, reasons, score = categorize(all_text, all_html, country_cfg)
    if score < 20 and fuzz.partial_ratio(
        "digitalbyraa webdesign wordpress seo", all_text[:5000].lower()
    ) < 35:
        return None
    lead_angle = angle(cats, tech)
    sorted_emails  = sorted(contacts.keys())
    phone_region   = country_cfg.get("phone_region", country_code)
    combined       = all_html + " " + all_text
    contact_phones = pair_phones_to_contacts(contacts, combined, phone_region)
    contact_names  = pair_names_to_contacts(contacts, combined, all_html)
    return Lead(
        company=company_from_domain(dom), domain=dom, website=website,
        source_query=source_query, title=title, description=desc,
        emails=", ".join(sorted_emails),
        email_titles=", ".join(contacts.get(e, "") for e in sorted_emails),
        email_phones=normalize_phone_list(", ".join(contact_phones.get(e, "") for e in sorted_emails)),
        email_names=", ".join(contact_names.get(e, "") for e in sorted_emails),
        phones=normalize_phone_list(", ".join(sorted(phones))), contact_page=contact_page,
        linkedin=linkedin or linkedin_hints.get(website, ""),
        detected_tech=", ".join(sorted(tech)),
        categories=", ".join(sorted(cats)),
        reseller_score=score, priority=priority(score), reasons="; ".join(reasons),
        suggested_angle=lead_angle,
        country=country_code, country_name=country_cfg.get("name", country_code),
        crawled_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        found_by_search  = "yes" if source == "search"  else "",
        found_by_catalog = "yes" if source == "catalog" else "",
    )


# ---------------------------------------------------------------------------
# Batch crawl helpers
# ---------------------------------------------------------------------------

async def _run_batch_async(
    batch: list[tuple[str, str]], args, code: str, configs: dict,
    all_leads: list[Lead], export_path: Path, country_leads: dict,
    rejected_domains: set[str] | None = None,
    source: str = "search",     # propagated to each crawled Lead
) -> int:
    n = len(batch)
    print(f"\n  >>> Crawling {n} site{'s' if n > 1 else ''} in parallel <<<")
    timeout   = aiohttp.ClientTimeout(total=60, connect=10)
    connector = aiohttp.TCPConnector(limit=n, ssl=False)
    new_count = 0

    async def _crawl_with_dom(s, url, query):
        """Wrapper that returns (domain, lead) so rejected sites can be tracked."""
        dom = domain_of(url)
        result = await _async_crawl_site(
            s, url, query, args.max_pages, args.delay,
            code, configs.get(code, {}),
            min_score=getattr(args, "min_score", 50),
            source=source,
        )
        return dom, result

    # Dedicated 3-thread executor for Firestore writes — kept small so it never
    # exhausts the default executor that the crawl coroutines use.
    _write_exec = __import__("concurrent.futures", fromlist=["ThreadPoolExecutor"]).ThreadPoolExecutor(max_workers=3)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        min_score = getattr(args, "min_score", 50)
        force     = getattr(args, "force", False)
        no_fb     = getattr(args, "no_firebase", False)
        fb_col    = getattr(args, "firebase_collection", None)

        if source == "catalog":
            _blocklist: set[str] = set()
        else:
            _blocklist = set(load_lines(Path(__file__).parent.parent.parent / "config" / "blocklist_domains.txt"))

        tasks = [
            asyncio.create_task(asyncio.wait_for(_crawl_with_dom(session, url, query), timeout=120.0))
            for url, query in batch
            if not is_blocked(domain_of(url), _blocklist)
        ]

        # Pending Firestore writes — collected during crawl, flushed after
        pending_writes: list = []   # list of (fn, *args) tuples

        for coro in asyncio.as_completed(tasks):
            try:
                dom, lead = await coro
            except asyncio.TimeoutError:
                print(f"    [crawl timeout] site took >120s — skipping")
                continue
            except Exception as exc:
                print(f"    [crawl error]: {exc}")
                continue

            if not lead:
                if rejected_domains is not None:
                    rejected_domains.add(dom)
                if not force and not no_fb:
                    pending_writes.append((upsert_lead_excluded, dom,
                                           "crawl_failed_or_score_zero", f"https://{dom}/"))
                continue

            has_email = "yes" if lead.emails else "no"
            print(f"    -> {lead.priority} score={lead.reseller_score} email={has_email}  {lead.website}")

            if lead.reseller_score < min_score:
                print(f"       [skip] score {lead.reseller_score} < {min_score} threshold")
                if rejected_domains is not None:
                    rejected_domains.add(dom)
                if not force and not no_fb:
                    pending_writes.append((upsert_lead_excluded, dom,
                                           f"score {lead.reseller_score} < {min_score}", lead.website))
                continue

            all_leads.append(lead)
            country_leads[code] = country_leads.get(code, 0) + 1
            new_count += 1
            if not no_fb:
                pending_writes.append((upsert_lead, lead, fb_col))

        # Flush Firestore writes sequentially after crawl batch — no thread contention
        if pending_writes:
            print(f"    [firebase] writing {len(pending_writes)} records…", flush=True)
            loop = asyncio.get_running_loop()
            for pw in pending_writes:
                fn, *pw_args = pw
                if fn is upsert_lead:
                    _lead, _col = pw_args[0], pw_args[1] if len(pw_args) > 1 else None
                    try:
                        await asyncio.wait_for(
                            loop.run_in_executor(_write_exec, lambda l=_lead, c=_col: upsert_lead(l, collection=c)),
                            timeout=15.0,
                        )
                    except Exception as exc:
                        print(f"    [firebase] lead write error: {exc}")
                else:
                    # upsert_lead_excluded(domain, reason, website)
                    try:
                        await asyncio.wait_for(
                            loop.run_in_executor(_write_exec, lambda a=pw_args: fn(*a)),
                            timeout=15.0,
                        )
                    except Exception:
                        pass
        _write_exec.shutdown(wait=False)
        if not getattr(args, "no_output", False):
            export(dedupe_leads(all_leads), export_path)
    return new_count


def _crawl_batch(
    batch: list[tuple[str, str]], args, code: str, configs: dict,
    all_leads: list[Lead], export_path: Path, country_leads: dict,
    rejected_domains: set[str] | None = None,
    source: str = "search",
) -> int:
    if not batch:
        return 0
    return asyncio.run(_run_batch_async(batch, args, code, configs,
                                        all_leads, export_path, country_leads,
                                        rejected_domains=rejected_domains,
                                        source=source))


# ---------------------------------------------------------------------------
# Search run orchestrator
# ---------------------------------------------------------------------------

def load_queries_for_countries(countries: list[str],
                                explicit_queries: str | None = None) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    if explicit_queries:
        for q in load_lines(Path(explicit_queries)):
            pairs.append((q, "AUTO"))
        return pairs
    # Queries are stored in config/countries.json under each country's "queries" list
    configs = load_country_configs()
    for code in countries:
        country_cfg = configs.get(code, {})
        queries = country_cfg.get("queries", [])
        if not queries:
            print(f"  [queries] WARNING: no queries found for {code} in countries.json")
        for q in queries:
            if q and not q.startswith("#"):
                pairs.append((q, code))
    return pairs


# ---------------------------------------------------------------------------
# Background crawl executor — search and scrape run independently
# ---------------------------------------------------------------------------

import threading
import queue as _queue
from functions.config import cfg

class _BackgroundCrawler:
    """Runs crawl batches in a background thread so the search loop never blocks.

    The search loop submits batches via submit(); the crawler thread processes
    them in order. Call wait() at the end to drain all queued batches.
    """

    def __init__(self):
        self._queue: "_queue.Queue" = _queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        self._error: Exception | None = None

    def _worker(self):
        while True:
            item = self._queue.get()
            if item is None:          # shutdown sentinel
                self._queue.task_done()
                break
            fn, args_tuple = item
            try:
                fn(*args_tuple)
            except Exception as exc:
                print(f"  [bg-crawl] error: {exc}")
                self._error = exc
            finally:
                self._queue.task_done()

    def submit(self, fn, *args):
        self._queue.put((fn, args))

    def wait(self, timeout: float = 300.0):
        """Block until all queued batches are done, or timeout (seconds) elapses.

        Uses a polling loop so the main thread stays responsive and can be
        interrupted with Ctrl-C. Default timeout = 300 s (5 min) per wait call.
        """
        import time as _time
        deadline = _time.monotonic() + timeout
        while not self._queue.empty() or self._thread.is_alive():
            if _time.monotonic() > deadline:
                print(f"  [bg-crawl] wait() timed out after {timeout:.0f}s — "
                      f"background thread may still be running a slow site",
                      flush=True)
                break
            _time.sleep(0.5)
        # Send shutdown sentinel (idempotent — ignored after thread exits)
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass
        self._thread.join(timeout=10.0)
        if self._error:
            raise self._error



def run(args) -> None:
    _bg_crawler = _BackgroundCrawler()
    configs     = load_country_configs()
    countries   = selected_countries(args.countries, configs) or DEFAULT_COUNTRIES
    query_pairs = load_queries_for_countries(countries, args.queries or None)
    blocklist   = set(load_lines(Path(__file__).parent.parent.parent / "config" / "blocklist_domains.txt"))

    all_leads: list[Lead] = []
    # seen_domains is seeded exclusively from Firestore — no local CSV read.
    seen_domains: set[str] = getattr(args, "preloaded_domains", set()).copy()
    rejected_domains: set[str] = set()  # crawled-and-rejected this run — never re-queue
    if seen_domains:
        print(f"  [firebase] {len(seen_domains)} already-handled domains loaded from Firestore — will skip")

    queues: dict[str, list[str]] = defaultdict(list)
    for q, c in query_pairs:
        queues[c].append(q)

    country_leads:  dict[str, int] = {c: 0 for c in countries}
    country_streak: dict[str, int] = {c: 0 for c in countries}
    country_done:   set[str]       = set()
    total_queries_run = 0
    batch_size: int = args.workers
    pending: dict[str, list[tuple[str, str]]] = {c: [] for c in countries}

    print(f"Countries: {', '.join(countries)}")
    print(f"Batch size (parallel crawlers): {batch_size}")
    if args.max_country:
        print(f"Target: {args.max_country} leads/country  |  Give up after {args.give_up_after} empty queries")
    else:
        print(f"No per-country cap  |  Give up after {args.give_up_after} empty queries")

    # --- GitHub org pre-pass: one search per country, prepend results to queue ---
    if not getattr(args, "no_github", False):
        print("\n[GitHub] Pre-pass: searching for agency orgs on GitHub...")
        for code in countries:
            cfg      = configs.get(code, {})
            gh_urls  = github_org_search(cfg, code, max_orgs=200)
            inserted = 0
            for raw in gh_urls:
                dom = domain_of(normalize_url(raw))
                detected = country_for_domain(dom, countries, configs)
                if (not is_blocked(dom, blocklist) and dom
                        and dom not in seen_domains
                        and (detected is None or detected == code)):
                    pending[code].append((normalize_url(raw), f"github:{code}"))
                    seen_domains.add(dom)
                    inserted += 1
            if inserted:
                print(f"  [{code}] {inserted} GitHub orgs queued for crawling")
                if pending[code]:  # flush GitHub orgs immediately
                    batch, pending[code] = pending[code][:batch_size], pending[code][batch_size:]
                    _crawl_batch(batch, args, code, configs, all_leads, Path(args.output), country_leads,
                             rejected_domains=rejected_domains)

    def _flush(code: str, label: str = "") -> None:
        if not pending[code]:
            return
        batch, pending[code] = pending[code], []
        if label:
            print(f"  [{code}] {label}")
        # Wait for any background crawls to finish before final flush
        _bg_crawler.wait()
        _crawl_batch(batch, args, code, configs, all_leads, Path(args.output), country_leads,
                     rejected_domains=rejected_domains)

    while len(country_done) < len(countries):
        made_progress = False
        for code in countries:
            if code in country_done:
                continue
            if args.max_country and country_leads[code] >= args.max_country:
                # Drain any already-queued sites before stopping
                if pending[code]:
                    _flush(code, f"Draining {len(pending[code])} queued sites before exit")
                print(f"\n[{code}] Target of {args.max_country} leads reached.")
                country_done.add(code)
                continue
            if not queues[code]:
                _flush(code, f"Final batch of {len(pending[code])} sites")
                _bg_crawler.wait()
                print(f"\n[{code}] No more queries — giving up.")
                country_done.add(code)
                continue

            query = queues[code].pop(0)
            total_queries_run += 1
            print(f"\n[{code} | leads={country_leads[code]} | streak={country_streak[code]} | queue={len(pending[code])}] Query: {query}")

            # Run Brave + Bing in parallel (Google CSE as optional bonus), merge results
            brave_urls  = brave_search(query, args.max_results, country_code=code)
            bing_urls   = bing_search(query, args.max_results, exclude_domains=seen_domains)
            google_urls = google_cse_search(query, args.max_results)

            seen_u: set[str] = set()
            urls: list[str] = []
            for u in brave_urls + bing_urls + google_urls:
                if u not in seen_u:
                    seen_u.add(u)
                    urls.append(u)
            print(f"  Search: brave={len(brave_urls)} bing={len(bing_urls)} google={len(google_urls)} merged={len(urls)}")

            added = 0
            for raw in urls:
                url = clean_search_url(raw)
                # Only accept root-level URLs — skip any URL with a path beyond "/"
                # e.g. accept https://agency.com/ but reject https://agency.com/blog/post
                _parsed = urlparse(url)
                if _parsed.path not in ("", "/"):
                    continue
                # Skip bare IP-address hosts — e.g. https://185.169.252.47/
                if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", _parsed.hostname or ""):
                    continue
                dom = domain_of(url)
                # country_for_domain returns None for generic TLDs (.com/.net/.eu).
                # A None result means the domain doesn't belong to any *other* country,
                # so we accept it as a candidate for the current country query.
                detected_country = country_for_domain(dom, countries, configs)
                if (not is_blocked(dom, blocklist) and dom
                        and dom not in seen_domains
                        and dom not in rejected_domains
                        and (detected_country == code
                     or (detected_country is None and tld_accepted_for(dom, code, configs)))):
                    if args.max_country and len(pending[code]) + country_leads[code] >= args.max_country:
                        break
                    pending[code].append((url, query))
                    seen_domains.add(dom)
                    added += 1
            if added:
                print(f"  Added {added} URLs → queue now {len(pending[code])} pending")
                # Submit batches to background crawler (non-blocking)
                while len(pending[code]) >= batch_size:
                    batch, pending[code] = pending[code][:batch_size], pending[code][batch_size:]
                    _bg_crawler.submit(
                        _crawl_batch, batch, args, code, configs,
                        all_leads, Path(args.output), country_leads,
                        rejected_domains, "search"
                    )
                    print(f"  → Batch of {len(batch)} queued for crawling (background)  queue={len(pending[code])} remaining")

            else:
                # No new URLs from this query — increment give-up streak
                country_streak[code] += 1
                print(f"  No new URLs (streak={country_streak[code]}/{args.give_up_after})")
                if args.give_up_after and country_streak[code] >= args.give_up_after:
                    _flush(code, f"Final batch of {len(pending[code])} sites")
                    # Do NOT wait here — final _bg_crawler.wait() at end of run() drains all queued sites.
                    # Waiting here blocked the search loop for minutes on slow connections.
                    print(f"\n[{code}] {args.give_up_after} consecutive empty queries — giving up.")
                    country_done.add(code)
                    continue
            if added:
                country_streak[code] = 0
            made_progress = True

        if not made_progress:
            break

    # Final flush — drain any remaining pending sites for all countries
    for code in countries:
        if pending[code]:
            _flush(code, f"Final flush: {len(pending[code])} remaining sites")
    _bg_crawler.wait()

    print(f"\n{'='*60}")
    print(f"Search complete. Queries run: {total_queries_run}")
    for code in countries:
        print(f"  {code}: {country_leads.get(code, 0)} leads")
    print(f"{'='*60}")

    final_leads = dedupe_leads(all_leads)
    if getattr(args, "no_output", False):
        print(f"  [output] skipped (--no-output). {len(final_leads)} leads in memory.")
    else:
        export(final_leads, Path(args.output))
        print(f"Exported {len(final_leads)} leads to {args.output}/agency_leads.xlsx")
    return final_leads
