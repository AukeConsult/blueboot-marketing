"""site_sitemap_check.py -- Check sitemap for one or more URLs, optionally writing
results back to the site_leads Firestore collection.

Usage
-----
    # Dry run (just print):
    python app/site_sitemap_check.py https://hammerfest.kommune.no
    python app/site_sitemap_check.py https://afk.no https://nko.no --debug

    # Write results to site_leads (real update):
    python app/site_sitemap_check.py https://hammerfest.kommune.no --update
    python app/site_sitemap_check.py --domains hammerfest.kommune.no,afk.no --update

    # Read domains from a Firestore campaign:
    python app/site_sitemap_check.py --campaign norske-fylkeskommuner --update --limit 20

Options
-------
    urls            One or more URLs to check (positional)
    --domains       Comma-separated domain names to look up in site_leads
    --campaign      Read URLs from a Firestore campaign's campaign_leads subcollection
    --limit / -n    Max leads to process when using --campaign or --domains (default 50)
    --workers / -w  Concurrent fetches (default 5)
    --update        Write sitemap results back to Firestore site_leads (default: dry run)
    --debug / -d    Show per-URL sitemap trace (robots.txt, candidates, etc.)
"""
from __future__ import annotations

import threading as _threading
_local_fb_lock = _threading.Lock()

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _pathsetup  # noqa: F401

import aiohttp

from site_agent import read_sitemap_async

SITEMAP_TIMEOUT = 120.0

# ---------------------------------------------------------------------------
# Firestore helpers (optional — only when --update or --domains/--campaign)
# ---------------------------------------------------------------------------

def _get_db():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    try:
        import firebase_admin
        from firebase_admin import firestore
        import firebase_admin.credentials as fb_creds
        from functions.firebase_cred import get_firebase_cred
    except ImportError as exc:
        print(f"[sitemap-check] Cannot import Firebase: {exc}", file=sys.stderr)
        sys.exit(1)

    cred = get_firebase_cred()
    with _local_fb_lock:
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
    return firestore.client()


def _urls_from_domains(db, domains: list[str], limit: int) -> list[dict]:
    col = db.collection("site_leads")
    rows = []
    for dom in domains:
        dom = dom.lower().lstrip("www.")
        q = col.where("domain", "==", dom).limit(1)
        for doc in q.stream():
            d = doc.to_dict() or {}
            url = d.get("website") or f"https://{dom}"
            rows.append({"lead_id": doc.id, "url": url, "collection": "site_leads"})
            if len(rows) >= limit:
                return rows
    return rows


def _urls_from_campaign(db, campaign_id: str, limit: int) -> list[dict]:
    col = (db.collection("campaigns")
             .document(campaign_id)
             .collection("campaign_leads"))
    rows = []
    for doc in col.stream():
        d = doc.to_dict() or {}
        url = (d.get("website") or "").strip()
        if not url:
            continue
        rows.append({"lead_id": doc.id, "url": url, "collection": f"campaigns/{campaign_id}/campaign_leads"})
        if len(rows) >= limit:
            break
    return rows


def _write_result(db, row: dict, result: dict) -> None:
    coll_path = row.get("collection", "site_leads")
    ref = db.document(f"{coll_path}/{row['lead_id']}")
    ref.set(result, merge=True)


# ---------------------------------------------------------------------------
# Core async runner
# ---------------------------------------------------------------------------

async def _run_async(rows: list[dict], concurrency: int, debug: bool,
                     update: bool, db) -> None:
    sem = asyncio.Semaphore(concurrency)
    loop = asyncio.get_running_loop()
    total = len(rows)
    connector = aiohttp.TCPConnector(ssl=False, limit=concurrency * 3)
    session_timeout = aiohttp.ClientTimeout(total=45, connect=8)

    async def _one(i: int, row: dict) -> None:
        url = row["url"]
        if not url.startswith("http"):
            url = "https://" + url

        async with sem:
            t0 = time.monotonic()
            try:
                count, s_url, s_type, sitemaps, oldest, newest, platform = \
                    await asyncio.wait_for(
                        read_sitemap_async(session, url, debug=debug),
                        timeout=SITEMAP_TIMEOUT,
                    )
            except asyncio.TimeoutError:
                print(f"  [{i}/{total}] TIMEOUT  {url}")
                return
            except Exception as exc:
                print(f"  [{i}/{total}] ERROR  {url}: {exc}")
                return
            elapsed = time.monotonic() - t0

        short_url = (s_url or "—")[:55]
        print(f"  [{i}/{total}] {url:<48}  pages={count:>5}  ({s_type:<7})  {short_url}  {elapsed:.1f}s")
        for s in sitemaps:
            print(f"           {s['url']}  pages={s['page_count']}")

        if update and db is not None:
            updates = {
                "sitemap_url":         s_url,
                "sitemap_type":        s_type,
                "page_count":          count,
                "sitemaps":            sitemaps,
                "sitemap_oldest_date": oldest,
                "sitemap_newest_date": newest,
                "platform":            platform,
            }
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, lambda r=row, u=updates: _write_result(db, r, u)),
                    timeout=12.0,
                )
                print(f"           → written to Firestore")
            except Exception as exc:
                print(f"           → write FAILED: {exc}")

    async with aiohttp.ClientSession(connector=connector, timeout=session_timeout) as session:
        tasks = [asyncio.create_task(_one(i + 1, row)) for i, row in enumerate(rows)]
        await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Check sitemap for one or more URLs; optionally write results to Firestore"
    )
    ap.add_argument("urls", nargs="*", help="One or more URLs to check")
    ap.add_argument("--domains", metavar="NAMES",
                    help="Comma-separated domain names to look up in site_leads")
    ap.add_argument("--campaign", "-c", metavar="CAMPAIGN_ID",
                    help="Read URLs from Firestore campaign_leads")
    ap.add_argument("--limit", "-n", type=int, default=50,
                    help="Max leads when using --domains/--campaign (default 50)")
    ap.add_argument("--workers", "-w", type=int, default=5,
                    help="Concurrent fetches (default 5)")
    ap.add_argument("--update", action="store_true",
                    help="Write results back to Firestore (default: dry run, just print)")
    ap.add_argument("--debug", "-d", action="store_true",
                    help="Show sitemap fetch trace (robots.txt, candidates, etc.)")
    args = ap.parse_args(argv)

    db = None
    rows: list[dict] = []

    if args.urls:
        rows = [{"lead_id": f"cli_{i}", "url": u, "collection": "site_leads"}
                for i, u in enumerate(args.urls)]

    if args.domains or args.campaign or args.update:
        db = _get_db()

    if args.domains:
        domains = [d.strip() for d in args.domains.replace(";", ",").split(",") if d.strip()]
        rows += _urls_from_domains(db, domains, args.limit)

    if args.campaign:
        rows += _urls_from_campaign(db, args.campaign, args.limit)

    if not rows:
        ap.error("Provide at least one URL, --domains, or --campaign")

    mode = "UPDATING Firestore" if args.update else "DRY RUN (no writes)"
    print(f"[sitemap-check] {len(rows)} URL(s)  |  {mode}  |  workers={args.workers}\n")

    asyncio.run(_run_async(rows, args.workers, args.debug, args.update, db))


if __name__ == "__main__":
    main()
