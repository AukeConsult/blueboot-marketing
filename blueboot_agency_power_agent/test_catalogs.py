#!/usr/bin/env python3
"""
Test all catalog entries for a country to see which URLs actually work.

Usage (run from blueboot_agency_power_agent root):
    cd app && python ../test_catalogs.py --country NO
    cd app && python ../test_catalogs.py --country NO --new-only
"""
from __future__ import annotations
import argparse, json, sys, time
import requests
from pathlib import Path
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(Path(__file__).parent / "app"))
from utils import domain_of, is_blocked, load_lines

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
H  = {"User-Agent": UA, "Accept-Language": "en;q=0.8", "Accept": "text/html,*/*;q=0.8"}


def count_external_links(html: str, base_url: str, blocklist: set) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    found, seen = [], set()
    catalog_dom = domain_of(base_url)
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith("http"):
            href = urljoin(base_url, href)
        dom = domain_of(href)
        if dom and dom != catalog_dom and not is_blocked(dom, blocklist):
            home = f"{urlparse(href).scheme}://{urlparse(href).netloc}/"
            if home not in seen:
                seen.add(home)
                found.append(home)
    return found


def test_entry(e: dict, blocklist: set) -> str:
    """Returns status string: OK / 404 / BOT / JS / ERR"""
    url = e["url"].replace("{page}", "1").replace("{offset}", "0")
    label = e["name"]
    try:
        r = requests.get(url, headers=H, timeout=18, allow_redirects=True)
        size = len(r.text)
        if r.status_code == 404:
            return f"404   {label}"
        if r.status_code != 200:
            return f"{r.status_code}   {label}"
        if size < 8_000:
            return f"BOT   {label}  ({size:,}B — bot challenge)"

        links = count_external_links(r.text, url, blocklist)
        is_js = any(x in r.text for x in ["__NEXT_DATA__", "data-reactroot", "__nuxt", "ng-app", "window.__APP__"])
        render = "JS " if is_js else "SR "  # SR = server-rendered

        if links:
            return f"OK    {label}  [{render}] {len(links)} ext links  ({size:,}B)"
        else:
            return f"EMPTY {label}  [{render}] 0 links — JS-rendered or no agencies listed  ({size:,}B)"
    except Exception as ex:
        return f"ERR   {label}: {ex}"


def main():
    ap = argparse.ArgumentParser(description="Test catalog URL entries.")
    ap.add_argument("--country", default="NO", help="ISO country code (default: NO)")
    ap.add_argument("--new-only", action="store_true", help="Only test entries marked __new_dir")
    ap.add_argument("--delay", type=float, default=0.8)
    args = ap.parse_args()

    root = Path(__file__).parent
    data = json.loads((root / "config/catalogs.json").read_text("utf-8"))
    blocklist = set(load_lines(root / "config/blocklist_domains.txt"))

    entries = [e for e in data.get(args.country.upper(), []) if isinstance(e, dict)]
    if args.new_only:
        entries = [e for e in entries if e.get("__new_dir")]

    print(f"\nTesting {len(entries)} catalog entries for {args.country.upper()}"
          f"{' (new only)' if args.new_only else ''}...\n")

    ok, empty_js, bot, errors = [], [], [], []

    for e in entries:
        result = test_entry(e, blocklist)
        print(f"  {result}")
        name = e["name"]
        if result.startswith("OK"):   ok.append(name)
        elif result.startswith("EMPTY"): empty_js.append(name)
        elif result.startswith("BOT"):   bot.append(name)
        else:                            errors.append(name)
        time.sleep(args.delay)

    print(f"\n{'='*60}")
    print(f"WORKING  : {len(ok)}")
    print(f"EMPTY/JS : {len(empty_js)}  (no links — likely JS-rendered)")
    print(f"BOT      : {len(bot)}  (bot-challenged)")
    print(f"ERROR    : {len(errors)}  (404 / connection error)")
    if ok:
        print(f"\n✓ Working sources:")
        for n in ok: print(f"    {n}")
    if empty_js:
        print(f"\n? Empty/JS sources (verify manually):")
        for n in empty_js: print(f"    {n}")
    if errors:
        print(f"\n✗ Dead/errored sources (consider removing):")
        for n in errors: print(f"    {n}")


if __name__ == "__main__":
    main()
