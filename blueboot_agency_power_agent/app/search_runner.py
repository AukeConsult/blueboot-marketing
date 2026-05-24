"""Search mode — Bing/Google search, site crawling, batch orchestration, run()."""
from __future__ import annotations

import asyncio
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

from utils import (
    USER_AGENT, BROWSER_UA,
    normalize_url, domain_of, company_from_domain,
    is_product_or_content_url, is_blocked, allowed_domain, country_for_domain,
    fetch, extract_meta, visible_text, extract_contacts, extract_phones,
    pair_phones_to_contacts, pair_names_to_contacts, extract_links, detect_tech, categorize, priority, angle,
    load_lines, load_country_configs, selected_countries, DEFAULT_COUNTRIES,
    linkedin_hints,
)
from firebase_sync import upsert_lead
from models import Lead, dedupe_leads, export, load_existing_leads


# ---------------------------------------------------------------------------
# Search backends
# ---------------------------------------------------------------------------

def bing_search(query: str, max_results: int,
                exclude_domains: set[str] | None = None) -> list[str]:
    """Search Bing via RSS feed."""
    import xml.etree.ElementTree as ET
    q = query
    if exclude_domains:
        q += " " + " ".join(f"-site:{d}" for d in list(exclude_domains)[:20])
    urls, seen, page = [], set(), 1
    while len(urls) < max_results:
        first = (page - 1) * 100 + 1
        try:
            resp = requests.get(
                "https://www.bing.com/search",
                params={"q": q, "format": "rss", "count": 100, "first": first},
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
        email_phones=", ".join(contact_phones.get(e, "") for e in sorted_emails),
        email_names=", ".join(contact_names.get(e, "") for e in sorted_emails),
        phones=", ".join(sorted(phones)), contact_page=contact_page,
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
        email_phones=", ".join(contact_phones.get(e, "") for e in sorted_emails),
        email_names=", ".join(contact_names.get(e, "") for e in sorted_emails),
        phones=", ".join(sorted(phones)), contact_page=contact_page,
        linkedin=linkedin or linkedin_hints.get(website, ""),
        detected_tech=", ".join(sorted(tech)),
        categories=", ".join(sorted(cats)),
        reseller_score=score, priority=priority(score), reasons="; ".join(reasons),
        suggested_angle=lead_angle,
        country=country_code, country_name=country_cfg.get("name", country_code),
        crawled_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


# ---------------------------------------------------------------------------
# Batch crawl helpers
# ---------------------------------------------------------------------------

async def _run_batch_async(
    batch: list[tuple[str, str]], args, code: str, configs: dict,
    all_leads: list[Lead], export_path: Path, country_leads: dict,
) -> int:
    n = len(batch)
    print(f"\n  >>> Crawling {n} site{'s' if n > 1 else ''} in parallel <<<")
    timeout   = aiohttp.ClientTimeout(total=60, connect=10)
    connector = aiohttp.TCPConnector(limit=n, ssl=False)
    new_count = 0
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        tasks = [
            asyncio.create_task(
                _async_crawl_site(session, url, query, args.max_pages,
                                  args.delay, code, configs.get(code, {}))
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
                if not getattr(args, "no_firebase", False):
                    upsert_lead(lead, collection=getattr(args, "firebase_collection", None))
                if not getattr(args, "no_output", False):
                    export(dedupe_leads(all_leads), export_path)
    return new_count


def _crawl_batch(
    batch: list[tuple[str, str]], args, code: str, configs: dict,
    all_leads: list[Lead], export_path: Path, country_leads: dict,
) -> int:
    if not batch:
        return 0
    return asyncio.run(_run_batch_async(batch, args, code, configs,
                                        all_leads, export_path, country_leads))


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
    for code in countries:
        for q in load_lines(Path(f"config/queries_{code}.txt")):
            pairs.append((q, code))
    return pairs


def run(args) -> None:
    configs     = load_country_configs()
    countries   = selected_countries(args.countries, configs) or DEFAULT_COUNTRIES
    query_pairs = load_queries_for_countries(countries, args.queries or None)
    blocklist   = set(load_lines(Path("config/blocklist_domains.txt")))

    all_leads: list[Lead] = load_existing_leads(Path(args.output))
    seen_domains: set[str] = {l.domain.strip().lower() for l in all_leads if l.domain}

    preloaded = getattr(args, "preloaded_domains", set())
    if preloaded:
        before = len(seen_domains)
        seen_domains |= preloaded
        print(f"  [firebase] added {len(seen_domains) - before} new domains from Firestore preload to skip list")

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

    def _flush(code: str, label: str = "") -> None:
        if not pending[code]:
            return
        batch, pending[code] = pending[code], []
        if label:
            print(f"  [{code}] {label}")
        _crawl_batch(batch, args, code, configs, all_leads, Path(args.output), country_leads)

    while len(country_done) < len(countries):
        made_progress = False
        for code in countries:
            if code in country_done:
                continue
            if args.max_country and country_leads[code] >= args.max_country:
                print(f"\n[{code}] Target of {args.max_country} leads reached.")
                country_done.add(code)
                continue
            if not queues[code]:
                _flush(code, f"Final batch of {len(pending[code])} sites")
                print(f"\n[{code}] No more queries — giving up.")
                country_done.add(code)
                continue

            query = queues[code].pop(0)
            total_queries_run += 1
            print(f"\n[{code} | leads={country_leads[code]} | streak={country_streak[code]}] Query: {query}")

            google_urls = google_cse_search(query, args.max_results)
            if google_urls:
                print(f"  Google: {len(google_urls)} results")
                urls = google_urls
            else:
                bing_urls = bing_search(query, args.max_results, exclude_domains=seen_domains)
                print(f"  Bing: {len(bing_urls)} results")
                urls = bing_urls

            added = 0
            for raw in urls:
                url = clean_search_url(raw)
                dom = domain_of(url)
                detected_country = country_for_domain(dom, countries, configs) or code
                if is_product_or_content_url(url):
                    p = urlparse(url)
                    url = f"{p.scheme}://{p.netloc}/"
                if (allowed_domain(dom, blocklist, countries, configs)
                        and dom not in seen_domains
                        and detected_country == code):
                    if args.max_country and len(pending[code]) + country_leads[code] >= args.max_country:
                        break
                    pending[code].append((url, query))
                    seen_domains.add(dom)
                    added += 1

            print(f"  -> {added} new candidates  (pending={len(pending[code])})")

            if len(pending[code]) >= batch_size:
                batch, pending[code] = pending[code][:batch_size], pending[code][batch_size:]
                _crawl_batch(batch, args, code, configs, all_leads, Path(args.output), country_leads)

            if added:
                country_streak[code] = 0
            else:
                country_streak[code] += 1
                print(f"  [{code}] Nothing new — streak {country_streak[code]}/{args.give_up_after}")
                if country_streak[code] >= args.give_up_after:
                    _flush(code, f"Final batch of {len(pending[code])} sites")
                    print(f"  [{code}] Giving up after {args.give_up_after} empty queries.")
                    country_done.add(code)

            made_progress = True

        if not made_progress:
            break

    print(f"\n{'='*60}")
    print(f"Finished. Ran {total_queries_run} queries total.")
    for code in countries:
        _flush(code, "Final")

    final_leads = dedupe_leads(all_leads)
    if getattr(args, "no_output", False):
        print(f"  [output] skipped (--no-output). {len(final_leads)} leads in memory.")
    else:
        export(final_leads, Path(args.output))
        print(f"Exported {len(final_leads)} leads to {args.output}/agency_leads.xlsx")
    return final_leads
