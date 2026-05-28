#!/usr/bin/env python3
"""
Sitemap reader smoke-test.

Tests read_sitemap_async against real sites to verify:
  - Multi-level sitemap indexes are followed
  - Google News sitemaps are skipped (vg.no returns ~273 without the fix)
  - Sub-sitemaps at any depth are counted

Run from the project root:
    python app/test_sitemap.py

Or from app/:
    python test_sitemap.py
"""
import asyncio
import sys
import types
from pathlib import Path

# ── path setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))   # app/
import _pathsetup  # noqa: F401  sets up root / functions / collect-functions

import aiohttp
import site_agent

# ── logging wrapper ───────────────────────────────────────────────────────────
# Monkey-patch _async_get so we can see every sitemap URL being fetched.
_original_async_get = site_agent._async_get
_fetch_log: list[str] = []

async def _logged_async_get(session, url, timeout=15, xml=False):
    _fetch_log.append(url)
    # Patch: call the real aiohttp directly so we can capture raw details
    import aiohttp as _aiohttp
    headers = {"User-Agent": site_agent._HTTP_HEADERS["User-Agent"]}
    if xml:
        headers["Accept"] = "application/xml,text/xml,*/*;q=0.8"
    raw_status = "?"
    raw_ct     = "?"
    raw_peek   = ""
    result     = ""
    try:
        async with session.get(url, headers=headers,
                               timeout=_aiohttp.ClientTimeout(total=timeout),
                               allow_redirects=True, ssl=False) as resp:
            raw_status = resp.status
            raw_ct     = resp.headers.get("content-type", "?")
            text = (await resp.text(errors="replace"))[:3_000_000]
            raw_peek = repr(text[:120])
            # apply same xml check as _async_get
            if resp.status == 200:
                if xml:
                    stripped = text.lstrip()
                    if (stripped.startswith("<?xml")
                            or stripped.startswith("<sitemapindex")
                            or stripped.startswith("<urlset")):
                        result = text
                else:
                    result = text
    except Exception as exc:
        raw_peek = f"EXCEPTION: {exc}"

    size_s = f"{len(result):,} chars" if result else "EMPTY"

    # Flag robots.txt
    extra = ""
    if url.endswith("robots.txt") and result:
        sm_lines = [l.strip() for l in result.splitlines()
                    if l.strip().lower().startswith("sitemap:")]
        if sm_lines:
            extra = "\n       Sitemaps: " + " | ".join(sm_lines)

    print(f"  [{len(_fetch_log):3d}] HTTP {raw_status}  {size_s:>12}  ct={raw_ct[:40]}")
    print(f"       url   : {url}")
    if raw_peek:
        print(f"       peek  : {raw_peek}")
    if extra:
        print(extra)
    return result

# ── test cases ────────────────────────────────────────────────────────────────
TESTS = [
    {
        "url":           "https://www.vg.no",
        "min_pages":     10_000,    # has 100k+ articles; news sitemap alone gives ~273
        "description":   "vg.no — large Norwegian news site with sub-sitemap hierarchy",
    },
    {
        "url":           "https://www.aftenposten.no",
        "min_pages":     5_000,
        "description":   "aftenposten.no — large Norwegian newspaper",
    },
    {
        "url":           "https://www.nrk.no",
        "min_pages":     5_000,
        "description":   "nrk.no — Norwegian public broadcaster",
    },
]

# ── runner ────────────────────────────────────────────────────────────────────
async def run_test(session, test: dict) -> bool:
    global _fetch_log
    _fetch_log = []

    site_agent._async_get = _logged_async_get

    url = test["url"]
    min_pages = test["min_pages"]
    print(f"\n{'='*60}")
    print(f"  {test['description']}")
    print(f"  URL: {url}")
    print(f"{'='*60}")

    count, sitemap_url, sitemap_type, sitemaps, oldest, newest, platform = await site_agent.read_sitemap_async(session, url)

    print()
    print(f"  page_count   : {count:,}")
    print(f"  sitemap_url  : {sitemap_url}")
    print(f"  sitemap_type : {sitemap_type}")
    print(f"  platform     : {platform or '(unknown)'}")
    print(f"  oldest       : {oldest or '(none)'}")
    print(f"  newest       : {newest or '(none)'}")
    print(f"  total fetches: {len(_fetch_log)}")
    if sitemaps:
        print(f"  sitemaps found ({len(sitemaps)}):")
        for s in sitemaps:
            print(f"    {s.get('filename','?'):45}  pages={s.get('page_count',0):>7,}  [{s.get('lastmod','')}]")

    passed = True

    if count >= min_pages:
        print(f"\n  PASS  {count:,} >= {min_pages:,}")
    else:
        print(f"\n  FAIL  {count:,} < {min_pages:,}  (unexpectedly low)")
        passed = False

    if sitemap_type != "none":
        print(f"  PASS  sitemap found (type={sitemap_type})")
    else:
        print(f"  FAIL  no sitemap found at all")
        passed = False

    # Check no news-only result snuck through
    if count > 0 and count < 500 and sitemap_type == "urlset":
        print(f"  WARN  count={count} looks like a news-only sitemap was returned")
        passed = False
    else:
        print(f"  PASS  count not in news-sitemap range")

    return passed


async def main():
    print("\nSitemap reader test suite")
    print(f"Testing {len(TESTS)} site(s)\n")

    connector = aiohttp.TCPConnector(ssl=False, limit=5)
    timeout   = aiohttp.ClientTimeout(total=45, connect=8)

    results = []
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        for test in TESTS:
            ok = await run_test(session, test)
            results.append((test["url"], ok))

    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    all_passed = True
    for url, ok in results:
        status = "PASS" if ok else "FAIL"
        print(f"  {status}  {url}")
        if not ok:
            all_passed = False

    print()
    if all_passed:
        print("All tests passed.")
        sys.exit(0)
    else:
        print("Some tests FAILED.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
