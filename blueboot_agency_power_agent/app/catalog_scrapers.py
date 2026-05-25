"""Catalog scrapers — one function per directory source + catalog_run orchestrator."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from utils import domain_of, is_blocked, fetch, linkedin_hints, load_lines, load_country_configs, selected_countries, DEFAULT_COUNTRIES
from models import Lead, dedupe_leads, export

CATALOG_CONFIG_PATH = Path("config/catalogs.json")


def load_catalogs(path: Path = CATALOG_CONFIG_PATH) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Generic fallback
# ---------------------------------------------------------------------------

def catalog_links_generic(url: str, blocklist: set[str]) -> list[str] | None:
    """Fetch a listing page and collect all outbound external links."""
    try:
        html = fetch(url, timeout=20, browser_ua=True)
    except Exception as e:
        print(f"    [catalog] fetch error: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    found, seen = [], set()
    catalog_dom = domain_of(url)
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith("http"):
            href = urljoin(url, href)
        link_dom = domain_of(href)
        if link_dom and link_dom != catalog_dom and not is_blocked(link_dom, blocklist):
            home = f"{urlparse(href).scheme}://{urlparse(href).netloc}/"
            if home not in seen:
                seen.add(home)
                found.append(home)
    return found


# ---------------------------------------------------------------------------
# Sortlist
# ---------------------------------------------------------------------------

def _sortlist_urls_from_json(obj, blocklist: set[str]) -> list[str]:
    """Walk a Sortlist Next.js/Apollo JSON, collect agency website URLs.
    Populates linkedin_hints as a side-effect."""
    URL_KEYS   = {"website","websiteurl","websiteuri","web","siteurl",
                  "externalurl","external_url","homepage","homepagelinks","link","links"}
    DOMAIN_KEYS = {"domain"}
    found, seen = [], set()

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
        v = v.strip().rstrip("/")
        if not v or " " in v or "." not in v or "/" in v or "sortlist" in v:
            return
        dom = domain_of("https://" + v)
        if not dom or is_blocked(dom, blocklist):
            return
        _add(f"https://{v}/")

    def _walk(node) -> None:
        if isinstance(node, dict):
            lc = {k.lower(): v for k, v in node.items()}
            if "website" in lc and isinstance(lc["website"], str):
                raw = lc["website"].strip()
                if raw.startswith("http"):
                    parsed = urlparse(raw)
                    home = f"{parsed.scheme}://{parsed.netloc}/"
                    dom = domain_of(raw)
                    if dom and "sortlist" not in dom and not is_blocked(dom, blocklist):
                        li_url = ""
                        if "linkedin" in lc and isinstance(lc["linkedin"], str) and "linkedin.com" in lc["linkedin"]:
                            li_url = lc["linkedin"]
                        elif "socialprofiles" in lc and isinstance(lc["socialprofiles"], dict):
                            sp = {k.lower(): v for k, v in lc["socialprofiles"].items()}
                            if "linkedin" in sp and isinstance(sp["linkedin"], str):
                                li_url = sp["linkedin"]
                        if li_url and home not in linkedin_hints:
                            linkedin_hints[home] = li_url
            for k, v in node.items():
                kl = k.lower()
                if kl in URL_KEYS:
                    if isinstance(v, str): _collect_url(v)
                    else: _walk(v)
                elif kl in DOMAIN_KEYS:
                    if isinstance(v, str): _collect_domain(v)
                else:
                    _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(obj)
    return found


def catalog_links_sortlist(url: str, blocklist: set[str]) -> list[str] | None:
    """Sortlist Next.js SPA — parse __NEXT_DATA__, fall back to <a> scan.
    Uses minimal headers (no Sec-Fetch-*) to bypass bot detection.
    Returns [] on 404 (category doesn't exist for this country — stop cleanly).
    Returns None on connection/server errors (skip page, try next)."""
    _ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    try:
        r = requests.get(url, headers={"User-Agent": _ua, "Accept-Language": "en;q=0.8"},
                         timeout=20, allow_redirects=True)
        if r.status_code == 404:
            return []   # category not available for this country — stop source quietly
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"    [catalog/sortlist] fetch error: {e}")
        return None

    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if script and script.string:
        try:
            data = json.loads(script.string)
            found = _sortlist_urls_from_json(data, blocklist)
            if found:
                return found
            print("    [catalog/sortlist] __NEXT_DATA__ parsed OK but 0 URLs extracted")
        except json.JSONDecodeError as e:
            print(f"    [catalog/sortlist] JSON parse error: {e}")

    # Fallback: <a> scan
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
    return found


# ---------------------------------------------------------------------------
# DesignRush
# ---------------------------------------------------------------------------

def catalog_links_designrush(url: str, blocklist: set[str]) -> list[str] | None:
    """DesignRush — 2-step scraper.

    DesignRush listing pages contain agency *profile* links (/agency/profile/...)
    but not the agencies' actual websites (those are only on the profile page).
    Step 1: collect profile URLs from the listing page.
    Step 2: visit each profile and extract the external website link.
    Returns None on fetch error, [] when source is exhausted (404).
    """
    _ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    _h  = {"User-Agent": _ua, "Accept-Language": "en;q=0.8"}

    # Step 1 — fetch listing page
    try:
        r = requests.get(url, headers=_h, timeout=20, allow_redirects=True)
        if r.status_code == 404:
            return []   # no more pages — signal exhaustion so the loop stops
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"    [catalog/designrush] fetch error: {e}")
        return None

    if len(html) < 10_000:
        print(f"    [catalog/designrush] page too small ({len(html):,}B) — bot challenge, skipping")
        return None

    soup = BeautifulSoup(html, "html.parser")

    # Collect agency profile links from the listing
    profiles, seen_p = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith("http"):
            href = "https://www.designrush.com" + href
        # Profile pages match /agency/profile/ or /agencies/... patterns
        if "designrush.com" in href and (
            "/agency/profile/" in href or
            re.search(r"/agencies?/[^/]+/[^/]+$", href)
        ):
            href = href.split("?")[0]
            if href not in seen_p:
                seen_p.add(href)
                profiles.append(href)

    if not profiles:
        print(f"    [catalog/designrush] 0 profile links found on listing page — page may be JS-rendered or empty")
        return None   # treat as transient error, not exhaustion

    # Step 2 — visit each profile page and pull the agency's external website link
    found, seen_d = [], set()
    for purl in profiles[:30]:          # cap: 30 profiles per listing page
        try:
            pr = requests.get(purl, headers=_h, timeout=12, allow_redirects=True)
            if pr.status_code != 200:
                continue
        except Exception:
            continue
        for a in BeautifulSoup(pr.text, "html.parser").find_all("a", href=True):
            href = a["href"].strip()
            if not href.startswith("http"):
                continue
            dom = domain_of(href)
            if dom and "designrush" not in dom and not is_blocked(dom, blocklist) and dom not in seen_d:
                found.append(f"{urlparse(href).scheme}://{urlparse(href).netloc}/")
                seen_d.add(dom)
                break                   # one website per agency profile
        time.sleep(0.4)

    return found


# ---------------------------------------------------------------------------
# Other sources
# ---------------------------------------------------------------------------

def catalog_links_clutch(url: str, blocklist: set[str]) -> list[str] | None:
    try:
        html = fetch(url, timeout=20, accept_language="en-US,en;q=0.9", browser_ua=True)
    except Exception as e:
        print(f"    [catalog/clutch] fetch error: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    found, seen = [], set()
    catalog_dom = domain_of(url)
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith("http"):
            continue
        dom = domain_of(href)
        if dom and dom != catalog_dom and not is_blocked(dom, blocklist):
            home = f"{urlparse(href).scheme}://{urlparse(href).netloc}/"
            if home not in seen:
                seen.add(home)
                found.append(home)
    return found


def catalog_links_goodfirms(url: str, blocklist: set[str]) -> list[str] | None:
    _ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    _h  = {"User-Agent": _ua, "Accept-Language": "en;q=0.8"}
    try:
        r = requests.get(url, headers=_h, timeout=20, allow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        print(f"    [catalog/goodfirms] fetch error: {e}")
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    profiles, seen_p = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "/company/" in href:
            if href.startswith("/"):
                href = "https://www.goodfirms.co" + href
            href = href.split("?")[0].split("#")[0]
            if href not in seen_p:
                seen_p.add(href)
                profiles.append(href)
    if not profiles:
        return []
    found, seen_d = [], set()
    for purl in profiles:
        try:
            pr = requests.get(purl, headers=_h, timeout=12, allow_redirects=True)
            pr.raise_for_status()
        except Exception:
            continue
        for a in BeautifulSoup(pr.text, "html.parser").find_all("a", href=True):
            href = a["href"].strip()
            if not href.startswith("http"):
                continue
            dom = domain_of(href)
            if dom and dom != "goodfirms.co" and not is_blocked(dom, blocklist) and dom not in seen_d:
                found.append(f"{urlparse(href).scheme}://{urlparse(href).netloc}/")
                seen_d.add(dom)
                break
        time.sleep(0.5)
    return found


def catalog_links_gulesider(url: str, blocklist: set[str]) -> list[str] | None:
    try:
        html = fetch(url, timeout=20, browser_ua=True)
    except Exception as e:
        print(f"    [catalog/gulesider] fetch error: {e}")
        return None
    return _simple_external_links(url, html, blocklist)


def catalog_links_proff(url: str, blocklist: set[str]) -> list[str] | None:
    try:
        html = fetch(url, timeout=20, browser_ua=True)
    except Exception as e:
        print(f"    [catalog/proff] fetch error: {e}")
        return None
    return _simple_external_links(url, html, blocklist)


def catalog_links_yelp(url: str, blocklist: set[str]) -> list[str] | None:
    return catalog_links_generic(url, blocklist)


def catalog_links_pagesjaunes(url: str, blocklist: set[str]) -> list[str] | None:
    return catalog_links_generic(url, blocklist)


def catalog_links_paginasamarillas(url: str, blocklist: set[str]) -> list[str] | None:
    return catalog_links_generic(url, blocklist)


def _simple_external_links(url: str, html: str, blocklist: set[str]) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    found, seen = [], set()
    catalog_dom = domain_of(url)
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
    return found


# ---------------------------------------------------------------------------
# Dispatch table + page scraper
# ---------------------------------------------------------------------------

CATALOG_EXTRACTORS = {
    "clutch":           catalog_links_clutch,
    "sortlist":         catalog_links_sortlist,
    "designrush":       catalog_links_designrush,
    "goodfirms":        catalog_links_goodfirms,
    "gulesider":        catalog_links_gulesider,
    "proff":            catalog_links_proff,
    "yelp":             catalog_links_yelp,
    "pagesjaunes":      catalog_links_pagesjaunes,
    "paginasamarillas": catalog_links_paginasamarillas,
    "generic":          catalog_links_generic,
}


def scrape_catalog_page(entry: dict, page: int, blocklist: set[str]) -> list[str] | None:
    """Fetch one page of a catalog source.
    Returns None on fetch error (skip page), [] when exhausted (stop source)."""
    offset = (page - 1) * 10
    url = entry["url"].format(page=page, offset=offset)
    extractor = CATALOG_EXTRACTORS.get(entry.get("type", "generic"), catalog_links_generic)
    return extractor(url, blocklist)


# ---------------------------------------------------------------------------
# Catalog run orchestrator
# ---------------------------------------------------------------------------

def catalog_run(args) -> None:
    """Scrape directory catalogs, crawl extracted agency sites, export leads."""
    # Import here to avoid any circular concerns at module load time
    from search_runner import _crawl_batch

    configs  = load_country_configs()
    countries = selected_countries(args.countries, configs) or DEFAULT_COUNTRIES
    blocklist = set(load_lines(Path("config/blocklist_domains.txt")))

    all_catalogs = load_catalogs()
    catalogs = {c: all_catalogs[c] for c in countries if c in all_catalogs}
    if not catalogs:
        print(f"No catalog entries found for: {', '.join(countries)}")
        return

    all_leads: list[Lead] = []
    # seen_domains is seeded exclusively from Firestore — no local CSV read.
    seen_domains: set[str] = getattr(args, "preloaded_domains", set()).copy()
    if seen_domains:
        print(f"  [firebase] {len(seen_domains)} already-handled domains loaded from Firestore — will skip")

    country_leads: dict[str, int] = {}
    batch_size: int = args.workers
    max_pages = getattr(args, "max_catalog_pages", None)

    print(f"Countries: {', '.join(countries)}")
    print(f"Batch size (parallel crawlers): {batch_size}")

    for code, sources in catalogs.items():
        print(f"\n{'='*60}\n[{code}] {len(sources)} catalog source(s)")
        pending: list[tuple[str, str]] = []

        for entry in sources:
            name = entry.get("name", entry.get("url", "?"))
            total_pages = min(entry.get("pages", 1), max_pages) if max_pages else entry.get("pages", 1)
            print(f"\n  Source: {name} (up to {total_pages} pages)")

            for page in range(1, total_pages + 1):
                print(f"  Page {page}/{total_pages}", end=" ... ", flush=True)
                links = scrape_catalog_page(entry, page, blocklist)

                if links is None:
                    print("fetch error — skipping page, continuing...")
                    continue
                if not links:
                    msg = "no page for this country — skipping." if page == 1 else "catalog exhausted."
                    print(f"0 links — {msg}")
                    break

                new_links = []
                for url in links:
                    dom = domain_of(url)
                    if dom and dom not in seen_domains and not is_blocked(dom, blocklist):
                        seen_domains.add(dom)
                        new_links.append((url, name))
                print(f"{len(new_links)} new candidates (of {len(links)} found)")
                pending.extend(new_links)

                while len(pending) >= batch_size:
                    batch, pending = pending[:batch_size], pending[batch_size:]
                    _crawl_batch(batch, args, code, configs, all_leads, Path(args.output), country_leads, source="catalog")

                import time as _time
                _time.sleep(args.delay)

        if pending:
            print(f"\n  [{code}] Flushing final batch of {len(pending)} sites")
            _crawl_batch(pending, args, code, configs, all_leads, Path(args.output), country_leads, source="catalog")

        print(f"\n[{code}] Done — {country_leads.get(code, 0)} new leads from catalogs")

    print(f"\n{'='*60}\nCatalog run complete.")
    final_leads = dedupe_leads(all_leads)
    if getattr(args, "no_output", False):
        print(f"  [output] skipped (--no-output). {len(final_leads)} leads in memory.")
    else:
        export(final_leads, Path(args.output))
        print(f"Exported {len(final_leads)} leads to {args.output}/agency_leads.xlsx")
    return final_leads
