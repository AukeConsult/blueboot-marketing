"""test_site_enrich.py -- CLI tester for LeadEmailWorker (email scraping + sitemap reading).

Runs the full site-enrich pipeline against one or more URLs and prints
exactly what the Cloud Function would do, without touching Firestore.

Usage
-----
    python app/test_site_enrich.py https://example.com
    python app/test_site_enrich.py https://example.com https://other.com
    python app/test_site_enrich.py --file urls.txt
    python app/test_site_enrich.py --campaign norske-fylkeskommuner --limit 10
    python app/test_site_enrich.py --campaign norske-fylkeskommuner --force --limit 5
    python app/test_site_enrich.py --debug https://www.drammen.kommune.no/

With --campaign the script reads website URLs straight from
campaigns/{id}/campaign_leads in Firestore (needs GOOGLE_APPLICATION_CREDENTIALS
or functions-crm/config/serviceAccountKey.json).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

# Allow running from project root or from app/
sys.path.insert(0, str(Path(__file__).parent.parent / "functions-crm"))
sys.path.insert(0, str(Path(__file__).parent))   # so _pathsetup is importable
import _pathsetup  # noqa: F401 -- sets Windows event loop + project root on sys.path

import aiohttp

from crm.campaign_scrape_lib import LeadEmailWorker, BoundedFetcher, SITE_TIMEOUT


_BROWSER_UA = "BlueBootLeadAgent/1.1 (+https://blueboot.ai)"


# ---------------------------------------------------------------------------
# Single-URL test
# ---------------------------------------------------------------------------

async def test_one(session: aiohttp.ClientSession,
                   fetcher: BoundedFetcher,
                   lead_id: str,
                   url: str,
                   page_count_already: int | None = None,
                   email_scraped_at: str | None = None,
                   debug: bool = False) -> dict:
    if not url.startswith("http"):
        url = "https://" + url

    data = {
        "website":          url,
        "page_count":       page_count_already,
        "email_scraped_at": email_scraped_at,
    }

    print(f"\n{'='*64}")
    print(f"  {url}")
    if page_count_already:
        print(f"  (page_count already={page_count_already} -- sitemap will be skipped)")
    if email_scraped_at:
        print(f"  (email_scraped_at={email_scraped_at} -- emails will be skipped)")
    print(f"{'='*64}")

    t0 = time.monotonic()
    worker = LeadEmailWorker(session, fetcher, lead_id, data, debug=debug)
    result = await worker.run()
    elapsed = time.monotonic() - t0

    status_icon = "ok" if result.status == "ok" else "ERROR"
    print(f"  status    : {status_icon}", end="")
    if result.error:
        print(f"  -- {result.error}", end="")
    print()
    print(f"  elapsed   : {elapsed:.1f}s  (timeout={SITE_TIMEOUT}s)")
    print(f"  page_count: {worker.page_count if worker.page_count is not None else '(not updated)'}")

    if worker.found:
        print(f"  emails ({len(worker.found)}):")
        for email, name in worker.found.items():
            print(f"    {email:<45}  {name or '(no name)'}")
    else:
        print("  emails    : (none found)")

    return {
        "url":        url,
        "status":     result.status,
        "error":      result.error or "",
        "emails":     len(worker.found),
        "page_count": worker.page_count,
        "elapsed":    round(elapsed, 1),
    }


# ---------------------------------------------------------------------------
# Firestore loader (optional)
# ---------------------------------------------------------------------------

def _load_from_campaign(campaign_id: str, force: bool, limit: int) -> list[dict]:
    """Read campaign_leads from Firestore. Returns list of dicts with url/lead_id/etc."""
    try:
        from firestore_client import get_firestore
    except ImportError as exc:
        print(f"Cannot import firestore_client: {exc}", file=sys.stderr)
        sys.exit(1)

    db = get_firestore()
    col = (db.collection("campaigns")
             .document(campaign_id)
             .collection("campaign_leads"))

    rows = []
    for doc in col.stream():
        d = doc.to_dict() or {}
        url = (d.get("website") or "").strip()
        if not url:
            continue
        if (d.get("status") or "").lower() == "excluded":
            continue
        if not force and d.get("email_scraped_at") and d.get("page_count"):
            continue
        rows.append({
            "lead_id":          doc.id,
            "url":              url,
            "page_count":       d.get("page_count"),
            "email_scraped_at": d.get("email_scraped_at"),
        })
        if len(rows) >= limit:
            break

    print(f"[test-site-enrich] Loaded {len(rows)} leads from campaign '{campaign_id}'")
    return rows


# ---------------------------------------------------------------------------
# Main async runner
# ---------------------------------------------------------------------------

async def main_async(rows: list[dict], concurrency: int, debug: bool = False) -> list[dict]:
    connector = aiohttp.TCPConnector(limit=concurrency * 3, ssl=False)
    headers   = {"User-Agent": _BROWSER_UA, "Accept-Language": "en,da;q=0.9"}

    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        fetcher = BoundedFetcher(session, headers=headers)
        sem     = asyncio.Semaphore(concurrency)

        async def run_one(row):
            async with sem:
                return await test_one(
                    session, fetcher,
                    row["lead_id"], row["url"],
                    page_count_already=row.get("page_count"),
                    email_scraped_at=row.get("email_scraped_at"),
                    debug=debug,
                )

        results = await asyncio.gather(*[run_one(r) for r in rows])

    return list(results)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Test LeadEmailWorker (email scrape + sitemap) against URLs"
    )
    ap.add_argument("urls", nargs="*", help="One or more URLs to test")
    ap.add_argument("--file", "-f", help="File with one URL per line")
    ap.add_argument("--campaign", "-c", metavar="CAMPAIGN_ID",
                    help="Read leads from Firestore campaign_leads")
    ap.add_argument("--force", action="store_true",
                    help="With --campaign: include already-scraped leads")
    ap.add_argument("--limit", "-n", type=int, default=20,
                    help="Max leads to test with --campaign (default 20)")
    ap.add_argument("--workers", "-w", type=int, default=3,
                    help="Concurrent workers (default 3)")
    ap.add_argument("--debug", "-d", action="store_true",
                    help="Show sitemap reader trace (robots.txt, candidates tried, etc.)")
    args = ap.parse_args(argv)

    rows: list[dict] = []

    if args.campaign:
        rows = _load_from_campaign(args.campaign, args.force, args.limit)
    else:
        urls: list[str] = list(args.urls) if args.urls else []
        if args.file:
            for line in Path(args.file).read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
        if not urls:
            ap.error("Provide at least one URL, --file, or --campaign")
        rows = [{"lead_id": f"test_{i}", "url": u,
                 "page_count": None, "email_scraped_at": None}
                for i, u in enumerate(urls)]

    if not rows:
        print("No leads to test.")
        return

    results = asyncio.run(main_async(rows, concurrency=args.workers, debug=args.debug))

    # Summary
    print(f"\n{'='*64}")
    print(f"SUMMARY  ({len(results)} sites)")
    print(f"{'='*64}")
    print(f"  {'URL':<45}  {'status':<8}  {'emails':>6}  {'pages':>6}  {'s':>5}")
    print(f"  {'-'*45}  {'-'*8}  {'-'*6}  {'-'*6}  {'-'*5}")
    ok = err = 0
    for r in results:
        icon = "ok" if r["status"] == "ok" else "ERROR"
        pc   = r["page_count"] if r["page_count"] is not None else "-"
        print(f"  {r['url']:<45}  {icon:<8}  {r['emails']:>6}  {str(pc):>6}  {r['elapsed']:>5.1f}")
        if r["status"] == "ok":
            ok += 1
        else:
            err += 1
            if r["error"]:
                print(f"    -> {r['error']}")
    print(f"\n  {ok} ok, {err} errors  (total {len(results)})")


if __name__ == "__main__":
    main()
