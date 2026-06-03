"""Quick sitemap diagnostic for a single domain."""
import asyncio, sys, os
sys.path.insert(0, os.path.dirname(__file__))
import _pathsetup  # noqa

import aiohttp
from site_agent import read_sitemap_async

async def main(url: str):
    connector = aiohttp.TCPConnector(ssl=False)
    timeout   = aiohttp.ClientTimeout(total=45, connect=8)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        total, sitemap_url, sitemap_type, sitemaps, oldest, newest, platform = \
            await read_sitemap_async(session, url, debug=True)
        print(f"\n=== RESULT ===")
        print(f"  pages       : {total}")
        print(f"  sitemap_url : {sitemap_url}")
        print(f"  sitemap_type: {sitemap_type}")
        print(f"  platform    : {platform}")
        print(f"  sitemaps    : {len(sitemaps)}")
        for s in sitemaps:
            print(f"    {s['url']}  pages={s['page_count']}")

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "https://trondheimkunstmuseum.no"
    asyncio.run(main(target))
