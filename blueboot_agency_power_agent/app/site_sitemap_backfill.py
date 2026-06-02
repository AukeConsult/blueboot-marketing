"""site_sitemap_backfill.py -- Backfill sitemap_url / sitemap_type / page_count
for existing site_leads documents that are missing this data.

Reads site_leads from Firestore, identifies docs without sitemap_url (or with
sitemap_type=="none"), fetches the sitemap live using read_sitemap_async from
site_agent, then writes the three fields back with merge=True.

Fields updated per document:
  sitemap_url   -- canonical sitemap URL found (e.g. https://example.com/sitemap_index.xml)
  sitemap_type  -- "index" | "urlset" | "none"
  page_count    -- estimated total indexed pages

Usage:
    python app/site_sitemap_backfill.py
    python app/site_sitemap_backfill.py --countries NO,SE
    python app/site_sitemap_backfill.py --limit 200 --dry-run
    python app/site_sitemap_backfill.py --force           # re-fetch even if already set
    python app/site_sitemap_backfill.py --concurrent 15
"""
from __future__ import annotations

import threading as _threading
import argparse
import asyncio
import importlib.util
import os
from datetime import datetime, timezone
from pathlib import Path

import _pathsetup  # noqa: F401

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

COLLECTION_DEFAULT = "site_leads"
CONCURRENT_DEFAULT = 10     # parallel aiohttp sitemap fetches
SITEMAP_TIMEOUT    = 120.0  # hard ceiling per site (robots + index tree + urlsets)

# ---------------------------------------------------------------------------
# Secrets / Firestore init (sync)
# ---------------------------------------------------------------------------

def _load_secrets():
    secrets_path = Path(__file__).parent.parent / "blueboot_secrets.py"
    if not secrets_path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("blueboot_secrets", secrets_path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, "fireBaseAdminKey", None)
    except Exception as e:
        print(f"  [backfill] could not load blueboot_secrets: {e}")
        return None


def _init_firestore(fb_key_dict, collection: str):
    try:
        import firebase_admin
        from firebase_admin import firestore
        import firebase_admin.credentials as fb_creds
    except ImportError:
        raise RuntimeError("firebase-admin not installed — run: pip install firebase-admin")

    cred = (fb_creds.Certificate(fb_key_dict) if fb_key_dict
            else fb_creds.Certificate(os.getenv("FIREBASE_CREDENTIALS",
                                                "config/serviceAccountKey.json")))
    with _local_fb_lock:
        with _local_fb_lock:
            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred)

    db  = firestore.client()
    col = db.collection(collection)
    return db, col


# ---------------------------------------------------------------------------
# Firestore scan (sync)
# ---------------------------------------------------------------------------

def _stream_for_backfill(
    col,
    countries: list[str] | None,
    force:     bool,
    limit:     int | None,
    domains:   list[str] | None = None,
) -> list[tuple]:
    """Return [(doc_ref, doc_dict), ...] for leads that need sitemap data."""
    print("  [backfill] Scanning site_leads…")
    results: list[tuple] = []
    scanned = skipped = 0

    for doc in col.stream():
        scanned += 1
        data = doc.to_dict() or {}

        if domains:
            d = (data.get("domain") or "").lower().lstrip("www.")
            if not any(d == dom or d.endswith("." + dom) for dom in domains):
                continue

        if countries:
            c = (data.get("country") or "").upper()
            if c not in countries and c != "*":
                continue

        if not force:
            s_url   = data.get("sitemap_url", "")
            s_type  = data.get("sitemap_type", "")
            s_count = int(data.get("page_count") or 0)
            # Also re-process sites with page_count==0 — those were likely
            # collected before the news-sitemap or _index_entries bugs were fixed.
            if s_url and s_type and s_type != "none" and s_count > 0:
                skipped += 1
                continue

        website = data.get("website") or data.get("domain", "")
        if not website:
            skipped += 1
            continue

        results.append((doc.reference, data))
        if limit and len(results) >= limit:
            break

    print(
        f"  [backfill] {scanned} scanned → {len(results)} need update  "
        f"({skipped} skipped — already have sitemap)"
    )
    return results


# ---------------------------------------------------------------------------
# Async fetch + write
# ---------------------------------------------------------------------------

async def _run_async(
    to_process: list[tuple],
    concurrent: int,
    dry_run:    bool,
    debug:      bool = False,
) -> dict:
    """Fetch sitemaps concurrently; write back to Firestore."""
    try:
        import aiohttp as _aiohttp
    except ImportError:
        raise RuntimeError("aiohttp not installed — run: pip install aiohttp")

    from site_agent import read_sitemap_async  # noqa: PLC0415

    total    = len(to_process)
    sem      = asyncio.Semaphore(concurrent)
    loop     = asyncio.get_running_loop()
    counters = {"total": total, "done": 0, "updated": 0, "failed": 0, "none": 0}

    connector       = _aiohttp.TCPConnector(ssl=False, limit=concurrent + 5)
    session_timeout = _aiohttp.ClientTimeout(total=45, connect=8)

    async def _fetch_one(session, doc_ref, data: dict) -> None:
        website = data.get("website") or f"https://{data.get('domain', '')}"
        domain  = data.get("domain", website)

        async with sem:
            try:
                count, s_url, s_type, s_sitemaps, s_oldest, s_newest, s_platform = await asyncio.wait_for(
                    read_sitemap_async(session, website, debug=debug),
                    timeout=SITEMAP_TIMEOUT,
                )
            except asyncio.TimeoutError:
                counters["done"]   += 1
                counters["failed"] += 1
                print(f"    [{counters['done']}/{total}] TIMEOUT  {domain}")
                return
            except Exception as exc:
                counters["done"]   += 1
                counters["failed"] += 1
                print(f"    [{counters['done']}/{total}] ERR  {domain}: {exc}")
                return

        updates = {
            "sitemap_url":          s_url,
            "sitemap_type":         s_type,
            "page_count":           count,
            "sitemaps":         s_sitemaps,
            "sitemap_oldest_date":  s_oldest,
            "sitemap_newest_date":  s_newest,
            "platform":             s_platform,
        }
        counters["done"] += 1
        if s_type == "none":
            counters["none"] += 1
        s_url_short = (s_url or "—")[:60]
        print(
            f"    [{counters['done']}/{total}] {domain:<42}"
            f"  pages={count:,}  ({s_type})  {s_url_short}"
        )

        if not dry_run:
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(
                        None, lambda r=doc_ref, u=updates: r.set(u, merge=True)
                    ),
                    timeout=12.0,
                )
                counters["updated"] += 1
            except Exception as exc:
                print(f"    [backfill] write error {domain}: {exc}")
                counters["failed"] += 1
        else:
            counters["updated"] += 1

    async with _aiohttp.ClientSession(
        connector=connector, timeout=session_timeout
    ) as session:
        tasks = [
            asyncio.create_task(_fetch_one(session, ref, data))
            for ref, data in to_process
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    return counters


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def backfill_sitemaps(
    collection: str           = COLLECTION_DEFAULT,
    countries:  list[str] | None = None,
    domains:    list[str] | None = None,
    limit:      int | None    = None,
    concurrent: int           = CONCURRENT_DEFAULT,
    force:      bool          = False,
    dry_run:    bool          = False,
    debug:      bool          = False,
) -> None:
    fb_key  = _load_secrets()
    _, col  = _init_firestore(fb_key, collection)

    to_proc = _stream_for_backfill(col, countries, force, limit, domains)
    if not to_proc:
        print("  [backfill] Nothing to do.")
        return

    total = len(to_proc)
    print(f"\n  [backfill] Collection : {collection}")
    print(f"  [backfill] Concurrent : {concurrent} parallel fetches")
    print(f"  [backfill] Total leads: {total}")
    print(f"  [backfill] Dry run    : {dry_run}\n")

    started = datetime.now(timezone.utc)
    counters = asyncio.run(_run_async(to_proc, concurrent, dry_run, debug))
    elapsed  = (datetime.now(timezone.utc) - started).total_seconds()

    print(f"\n  [backfill] Done in {elapsed:.0f}s")
    print(f"  Total    : {counters['total']}")
    print(f"  Updated  : {counters['updated']}")
    print(f"  No sitemap found : {counters['none']}")
    print(f"  Failed   : {counters['failed']}")
    if dry_run:
        print("  (dry-run — nothing written to Firestore)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    p = argparse.ArgumentParser(
        description="Backfill sitemap_url / sitemap_type / page_count for existing site_leads"
    )
    p.add_argument("--collection", default=COLLECTION_DEFAULT, metavar="NAME",
                   help=f"Firestore collection  (default: {COLLECTION_DEFAULT})")
    p.add_argument("--countries",  default=None, metavar="CODES",
                   help="Comma-separated country codes  e.g. NO,SE  (default: all)")
    p.add_argument("--limit",      type=int, default=None, metavar="N",
                   help="Max leads to process")
    p.add_argument("--concurrent", type=int, default=CONCURRENT_DEFAULT, metavar="N",
                   help=f"Parallel fetches  (default: {CONCURRENT_DEFAULT})")
    p.add_argument("--domains",    default=None, metavar="NAMES",
                   help="Comma-separated domain names to process  e.g. vg.no,dagbladet.no")
    p.add_argument("--force",      action="store_true",
                   help="Re-fetch even for leads that already have sitemap_url set")
    p.add_argument("--dry-run",    action="store_true",
                   help="Print what would be updated without writing to Firestore")
    p.add_argument("--debug",      action="store_true",
                   help="Print per-URL sitemap fetch details (useful with --domains)")

    args = p.parse_args(argv)

    countries = None
    if args.countries:
        countries = [c.strip().upper() for c in args.countries.split(",") if c.strip()]

    domains = None
    if args.domains:
        domains = [d.strip().lower().lstrip("www.") for d in args.domains.split(",") if d.strip()]

    backfill_sitemaps(
        collection = args.collection,
        countries  = countries,
        domains    = domains,
        limit      = args.limit,
        concurrent = args.concurrent,
        force      = args.force,
        dry_run    = args.dry_run,
        debug      = args.debug,
    )


if __name__ == "__main__":
    main()
