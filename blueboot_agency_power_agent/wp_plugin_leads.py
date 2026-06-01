"""wp_plugin_leads.py — Discover leads from WordPress.org plugin catalogue.

Queries the WordPress.org Plugin API for plugins matching given search terms,
extracts author_url (the developer's website), filters by country TLD, and
prints/exports the resulting leads.

Config is loaded from config/wp_plugin_queries.json (country terms + TLDs).

Usage:
    # Dry-run — print results only, no export
    python wp_plugin_leads.py --countries UK IN --dry-run

    # Run with export to CSV
    python wp_plugin_leads.py --countries UK --out uk_wp_leads.csv

    # Override search terms for one country
    python wp_plugin_leads.py --countries IN --terms "razorpay" "woocommerce india"

    # Full scan for a single country with verbose output
    python wp_plugin_leads.py --countries IN --per-term 200 --verbose

    # List all configured countries
    python wp_plugin_leads.py --list-countries

Options:
    --countries     ISO codes to collect (e.g. UK IN NO SE). Default: UK IN
    --terms         Override search terms (applies to all specified countries)
    --per-term      Max plugins to fetch per search term (default 100, max 250)
    --dry-run       Print results only; skip CSV export
    --verbose       Print each matched lead as it is found
    --out FILE      Output CSV path (default: wp_plugin_leads.csv)
    --config FILE   Path to config JSON (default: config/wp_plugin_queries.json)
    --list-countries  List configured countries and exit
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = Path("config/wp_plugin_queries.json")

_FALLBACK_TERMS = [
    "wordpress agency", "woocommerce", "wordpress development",
    "booking plugin", "payment gateway", "seo plugin",
    "membership plugin", "ecommerce plugin", "form plugin", "page builder",
]

_FALLBACK_BLOCKED = {
    "wordpress.org", "wordpress.com", "gravatar.com", "github.com",
    "automattic.com", "w.org", "wp.com",
}


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict:
    """Load wp_plugin_queries.json; return empty dict on missing file."""
    if not path.exists():
        print(f"[wp-plugins] Config not found at {path} — using built-in defaults", file=sys.stderr)
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def country_tlds(config: dict, country: str) -> list[str]:
    return config.get("countries", {}).get(country, {}).get("tlds", [])


def country_terms(config: dict, country: str) -> list[str]:
    return config.get("countries", {}).get(country, {}).get("terms", _FALLBACK_TERMS)


def blocked_domains(config: dict) -> set[str]:
    return set(config.get("blocked_domains", [])) or _FALLBACK_BLOCKED

# ---------------------------------------------------------------------------
# WordPress.org Plugin API
# ---------------------------------------------------------------------------

WP_API = "https://api.wordpress.org/plugins/info/1.2/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; LeadBot/1.0)",
    "Accept": "application/json",
}


def _wp_search(term: str, per_page: int = 100, page: int = 1) -> list[dict]:
    """Search WP plugin directory. Returns list of plugin dicts with slug + basic metadata.

    Note: the query_plugins endpoint does NOT return author_url even when requested.
    Use _wp_plugin_details(slug) to fetch the author's actual website URL.
    """
    params = {
        "action": "query_plugins",
        "request[search]": term,
        "request[per_page]": per_page,
        "request[page]": page,
        "request[fields][author]": 1,
        "request[fields][tags]": 1,
        "request[fields][short_description]": 1,
        "request[fields][active_installs]": 1,
        "request[fields][last_updated]": 1,
    }
    t0 = time.time()
    try:
        r = requests.get(WP_API, params=params, headers=HEADERS, timeout=20)
        elapsed = time.time() - t0
        r.raise_for_status()
        data = r.json()
        plugins = data.get("plugins", []) if isinstance(data, dict) else []
        total   = data.get("info", {}).get("results", "?") if isinstance(data, dict) else "?"
        print(f"    [api/search] {term!r:38s}  {len(plugins)} results (total={total})  {elapsed:.1f}s", flush=True)
        return plugins
    except Exception as e:
        elapsed = time.time() - t0
        print(f"    [api/search] {term!r:38s}  ERROR {elapsed:.1f}s: {e}", file=sys.stderr, flush=True)
        return []


def _wp_plugin_details(slug: str) -> dict:
    """Fetch full plugin details including author_url via plugin_information endpoint."""
    params = {
        "action": "plugin_information",
        "request[slug]": slug,
        "request[fields][author_url]": 1,
        "request[fields][homepage]": 1,
        "request[fields][author]": 1,
    }
    try:
        r = requests.get(WP_API, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _fetch_author_urls(slugs: list[str], max_workers: int = 10) -> dict[str, str]:
    """Fetch author_url for a list of plugin slugs in parallel.

    Returns {slug: author_url}. Falls back to homepage if author_url is empty.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: dict[str, str] = {}

    def fetch_one(slug: str) -> tuple[str, str]:
        details = _wp_plugin_details(slug)
        url = (details.get("author_url") or details.get("homepage") or "").strip()
        return slug, url

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_one, s): s for s in slugs}
        for fut in as_completed(futures):
            slug, url = fut.result()
            results[slug] = url

    return results


def _domain_of(url: str) -> str:
    """Extract bare domain from URL, stripping leading www."""
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host.strip()
    except Exception:
        return ""


def _tld_match(domain: str, tlds: list[str]) -> bool:
    """Return True if domain ends with one of the given TLDs."""
    return any(domain.endswith(tld) for tld in tlds)


def _is_blocked(domain: str, blocked: set[str]) -> bool:
    return any(domain == b or domain.endswith("." + b) for b in blocked)


def collect_plugins(
    config:         dict,
    countries:      list[str],
    terms_override: list[str] | None,
    per_term:       int,
    verbose:        bool,
    debug:          bool = False,
) -> list[dict]:
    """Collect plugin author URLs filtered by country, return deduped lead list.

    TLD behaviour per country (set in config):
      tld_strict=true  — hard-reject domains whose TLD is not in the tlds list.
                         Use for UK / IN / AU where the TLD is a reliable signal.
      tld_strict=false — accept all TLDs; tld_match=True is flagged in the output
                         but nothing is dropped.  Use for NO/SE/DK/FI where most
                         developers publish under .com even though they are local.
    """
    blocked = blocked_domains(config)
    seen_domains: set[str] = set()
    leads: list[dict] = []

    for country in countries:
        cc         = country.upper()
        cc_cfg     = config.get("countries", {}).get(cc, {})
        terms      = terms_override or cc_cfg.get("terms", _FALLBACK_TERMS)
        tlds       = cc_cfg.get("tlds", [])
        tld_strict = cc_cfg.get("tld_strict", False)   # default: soft / preferred only
        label      = cc_cfg.get("label", cc)

        mode_info = ""
        if tlds:
            mode_info = "  tlds=" + str(tlds) + " [" + ("strict" if tld_strict else "preferred") + "]"
        print("", flush=True)
        print("[wp-plugins] ── " + label + " (" + cc + ") ──  "
              + str(len(terms)) + " terms  per_term=" + str(per_term) + mode_info, flush=True)

        country_new  = 0
        country_start = time.time()

        for t_idx, term in enumerate(terms, 1):
            print(f"  [{t_idx:>2}/{len(terms)}] querying: {term!r}", flush=True)
            plugins       = _wp_search(term, per_page=min(per_term, 250))
            matched       = 0
            skip_blocked  = 0
            skip_tld      = 0
            skip_dupe     = 0

            if debug and not plugins:
                print(f"    [debug] API returned 0 plugins for this term", flush=True)

            # Step 2: fetch author_url for each slug (not returned by search endpoint)
            slugs_to_fetch = [
                p.get("slug", "") for p in plugins if p.get("slug")
            ]
            if slugs_to_fetch:
                print(f"    [api/details] fetching author URLs for {len(slugs_to_fetch)} plugins…", flush=True)
                slug_urls = _fetch_author_urls(slugs_to_fetch)
            else:
                slug_urls = {}

            if debug and plugins:
                print(f"    [debug] First 3 slug->author_url mappings:", flush=True)
                for dp in plugins[:3]:
                    sl = dp.get("slug","")
                    print(f"      {sl:30s} -> {slug_urls.get(sl,'[empty]')}", flush=True)

            for p in plugins:
                slug       = p.get("slug", "")
                author_url = slug_urls.get(slug, "").strip()
                if not author_url or not author_url.startswith("http"):
                    continue

                domain = _domain_of(author_url)
                if not domain:
                    continue

                if _is_blocked(domain, blocked):
                    skip_blocked += 1
                    continue

                if domain in seen_domains:
                    skip_dupe += 1
                    continue

                # Strict TLD mode: hard reject if TLD does not match
                if tlds and tld_strict and not _tld_match(domain, tlds):
                    skip_tld += 1
                    continue

                seen_domains.add(domain)
                tld_match = bool(tlds and _tld_match(domain, tlds))

                lead = {
                    "domain":          domain,
                    "website":         author_url,
                    "author":          p.get("author", ""),
                    "plugin_name":     p.get("name", ""),
                    "plugin_slug":     p.get("slug", ""),
                    "active_installs": p.get("active_installs", 0),
                    "last_updated":    p.get("last_updated", ""),
                    "description":     (p.get("short_description") or "")[:200],
                    "tags":            ", ".join((p.get("tags") or {}).keys())[:100],
                    "source_term":     term,
                    "country":         cc,
                    "tld_match":       tld_match,
                    "source":          "wp_plugin_catalogue",
                }
                leads.append(lead)
                matched += 1
                country_new += 1

                if verbose:
                    flag     = " *" if tld_match else "  "
                    installs = p.get("active_installs", 0)
                    name     = (p.get("name") or "")[:45]
                    print("   " + flag + " " + domain[:40].ljust(40)
                          + "  [" + str(installs).rjust(6) + " installs]  " + name, flush=True)

            # Per-term summary line
            skip_parts = []
            if skip_blocked: skip_parts.append(str(skip_blocked) + " blocked")
            if skip_tld:     skip_parts.append(str(skip_tld) + " tld-filtered")
            if skip_dupe:    skip_parts.append(str(skip_dupe) + " dupes")
            skip_info = ("  skipped: " + ", ".join(skip_parts)) if skip_parts else ""
            running_total = len(leads)
            print(f"       -> {matched:>3} new leads  |  {skip_info if skip_info else 'no skips'}"
                  f"  |  running total: {running_total}", flush=True)

            time.sleep(0.3)

        country_elapsed = time.time() - country_start
        print(f"  └─ {cc} done: {country_new} new leads  ({country_elapsed:.0f}s)", flush=True)

    return leads



# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _print_summary(leads: list[dict], elapsed: float = 0.0) -> None:
    by_country: dict[str, int] = {}
    by_tld: dict[bool, int]    = {True: 0, False: 0}
    for lead in leads:
        c = lead["country"]
        by_country[c] = by_country.get(c, 0) + 1
        by_tld[lead.get("tld_match", False)] += 1

    print("", flush=True)
    print("=" * 60, flush=True)
    print(f"  RESULTS", flush=True)
    print(f"  Total unique leads : {len(leads)}", flush=True)
    print(f"  TLD-confirmed      : {by_tld[True]}  (country-specific TLD)", flush=True)
    print(f"  TLD-inferred       : {by_tld[False]}  (.com or other — via search terms)", flush=True)
    if elapsed:
        print(f"  Time elapsed       : {elapsed:.0f}s", flush=True)
    print("", flush=True)
    print("  By country:", flush=True)
    for c, n in sorted(by_country.items()):
        print(f"    {c:4s}: {n}", flush=True)
    print("=" * 60, flush=True)


def _export_csv(leads: list[dict], path: Path) -> None:
    if not leads:
        print("[wp-plugins] No leads to export.")
        return
    fieldnames = list(leads[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(leads)
    print(f"[wp-plugins] Exported {len(leads)} leads → {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        description="Discover leads from WordPress.org plugin catalogue"
    )
    p.add_argument("--countries",  nargs="+", default=["UK", "IN"],
                   metavar="CC", help="Country codes to collect (e.g. UK IN NO SE)")
    p.add_argument("--terms",      nargs="+", default=None,
                   metavar="TERM", help="Override search terms (applies to all countries)")
    p.add_argument("--per-term",   type=int, default=100,
                   help="Max plugins to fetch per search term (default 100, max 250)")
    p.add_argument("--dry-run",    action="store_true",
                   help="Print results only, skip CSV export")
    p.add_argument("--verbose",    action="store_true",
                   help="Print each matched lead as it is found")
    p.add_argument("--out",        default="wp_plugin_leads.csv",
                   help="Output CSV path (default: wp_plugin_leads.csv)")
    p.add_argument("--config",     default=str(DEFAULT_CONFIG_PATH),
                   help=f"Config JSON path (default: {DEFAULT_CONFIG_PATH})")
    p.add_argument("--list-countries", action="store_true",
                   help="List configured countries and exit")
    p.add_argument("--debug",       action="store_true",
                   help="Dump raw API fields for the first term (diagnose empty results)")
    args = p.parse_args(argv)

    cfg = load_config(Path(args.config))

    if args.list_countries:
        countries_cfg = cfg.get("countries", {})
        if countries_cfg:
            print(f"Countries configured in {args.config}:")
            for cc, v in sorted(countries_cfg.items()):
                tlds  = v.get("tlds", [])
                terms = v.get("terms", [])
                print(f"  {cc:4s}  tlds={tlds}  terms={len(terms)}")
        else:
            print("No countries configured — built-in fallback terms will be used.")
        return

    # Support both space-separated (--countries NO SE DK) and
    # comma-separated (--countries NO,SE,DK) input
    raw = []
    for token in args.countries:
        raw.extend(c.strip() for c in token.split(",") if c.strip())
    countries = [c.upper() for c in raw]
    start_time = time.time()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_terms = sum(
        len(cfg.get("countries", {}).get(c, {}).get("terms", _FALLBACK_TERMS))
        for c in countries
    )
    print("=" * 60, flush=True)
    print(f"  WP Plugin Lead Collector  —  {ts}", flush=True)
    print(f"  Countries  : {', '.join(countries)}", flush=True)
    print(f"  Total terms: {total_terms}  (per-term max: {args.per_term} plugins)", flush=True)
    print(f"  Config     : {args.config}", flush=True)
    print(f"  Output     : {'[dry-run]' if args.dry_run else args.out}", flush=True)
    print("=" * 60, flush=True)

    leads = collect_plugins(
        config=cfg,
        countries=countries,
        terms_override=args.terms,
        per_term=args.per_term,
        verbose=args.verbose,
        debug=args.debug,
    )

    _print_summary(leads, elapsed=time.time() - start_time)

    if args.dry_run:
        print("[wp-plugins] --dry-run: skipping export", flush=True)
        if leads:
            print("\nSample (first 10):", flush=True)
            for lead in leads[:10]:
                tld_flag = "*" if lead.get("tld_match") else " "
                domain   = lead["domain"]
                installs = lead["active_installs"]
                country  = lead["country"]
                author   = lead["author"][:35]
                print(f"  [{tld_flag}] {domain:40s}  {installs:>6} installs  [{country}]  {author}", flush=True)
    else:
        _export_csv(leads, Path(args.out))


if __name__ == "__main__":
    main()
