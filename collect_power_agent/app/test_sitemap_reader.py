"""test_sitemap_reader.py -- Standalone test for SitemapReader.

Run against one or more URLs and print page_count, sitemap_url, type,
platform, and each discovered sitemap. No Firestore, no API keys needed.

Usage
-----
    python app/test_sitemap_reader.py https://example.com
    python app/test_sitemap_reader.py https://example.com https://other.com
    python app/test_sitemap_reader.py https://example.com --debug
    python app/test_sitemap_reader.py --file urls.txt
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

# Allow running from project root or from app/
sys.path.insert(0, str(Path(__file__).parent.parent / "functions-crm"))

import aiohttp

from crm.sitemap_reader import SitemapReader


async def test_one(session: aiohttp.ClientSession, url: str, debug: bool) -> dict:
    if not url.startswith("http"):
        url = "https://" + url
    print(f"\n{'='*60}")
    print(f"  URL: {url}")
    print(f"{'='*60}")
    t0 = time.monotonic()
    try:
        pc, sitemap_url, sitemap_type, sitemaps, oldest, newest, platform = \
            await asyncio.wait_for(
                SitemapReader(session, url, debug=debug).read(),
                timeout=60.0,
            )
    except asyncio.TimeoutError:
        print("  TIMEOUT after 60s")
        return {"url": url, "error": "timeout"}
    except Exception as exc:
        print(f"  ERROR: {exc}")
        return {"url": url, "error": str(exc)}

    elapsed = time.monotonic() - t0

    print(f"  page_count   : {pc:,}")
    print(f"  sitemap_url  : {sitemap_url or '(none)'}")
    print(f"  sitemap_type : {sitemap_type}")
    print(f"  platform     : {platform or '(unknown)'}")
    print(f"  oldest date  : {oldest or '(none)'}")
    print(f"  newest date  : {newest or '(none)'}")
    print(f"  elapsed      : {elapsed:.1f}s")
    print(f"  sitemaps found ({len(sitemaps)}):")
    for sm in sitemaps[:20]:
        print(f"    {sm.get('page_count',0):>6,} pages  {sm.get('url','')}")
    if len(sitemaps) > 20:
        print(f"    ... and {len(sitemaps)-20} more")

    return {
        "url":          url,
        "page_count":   pc,
        "sitemap_url":  sitemap_url,
        "sitemap_type": sitemap_type,
        "platform":     platform,
        "sitemaps":     len(sitemaps),
        "elapsed":      round(elapsed, 1),
    }


async def main_async(urls: list[str], debug: bool) -> None:
    connector = aiohttp.TCPConnector(limit=10, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        results = []
        for url in urls:
            r = await test_one(session, url, debug)
            results.append(r)

    # Summary table
    if len(results) > 1:
        print(f"\n{'='*60}")
        print("SUMMARY")
        print(f"{'='*60}")
        print(f"  {'URL':<45} {'pages':>8}  {'type':<10}  {'platform'}")
        for r in results:
            if "error" in r:
                print(f"  {r['url']:<45} {'ERROR':>8}  {r['error']}")
            else:
                print(f"  {r['url']:<45} {r['page_count']:>8,}  "
                      f"{r['sitemap_type']:<10}  {r['platform'] or '-'}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Test SitemapReader against URLs")
    ap.add_argument("urls", nargs="*", help="One or more URLs to test")
    ap.add_argument("--file", "-f", help="File with one URL per line")
    ap.add_argument("--debug", "-d", action="store_true",
                    help="Print detailed sitemap-dbg trace")
    args = ap.parse_args(argv)

    urls: list[str] = list(args.urls)
    if args.file:
        for line in Path(args.file).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)

    if not urls:
        ap.error("Provide at least one URL or --file")

    asyncio.run(main_async(urls, args.debug))


if __name__ == "__main__":
    main()
