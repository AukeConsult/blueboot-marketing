"""site_excluded_recheck.py — Re-check sites in sites_excluded collection.

Reads every document from sites_excluded, re-runs the full site check
(sitemap scan + contact scrape) via process_site_async, and for sites that
now pass:
  - Writes the lead to site_leads
  - Deletes the document from sites_excluded

Sites that still fail remain in sites_excluded unchanged.

Useful after bug-fixes (e.g. HTML sitemap detection, new platform signals)
that may have caused previously valid sites to be incorrectly excluded.

Usage:
    python app/site_excluded_recheck.py
    python app/site_excluded_recheck.py --countries NO,SE
    python app/site_excluded_recheck.py --reason min_pages   # only re-check sites excluded for this reason
    python app/site_excluded_recheck.py --min-pages 0        # accept any site with a sitemap
    python app/site_excluded_recheck.py --limit 200 --dry-run
    python app/site_excluded_recheck.py --concurrent 15
"""
from __future__ import annotations

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

LEADS_COLLECTION    = "site_leads"
EXCLUDED_COLLECTION = "sites_excluded"
CONCURRENT_DEFAULT  = 50
MIN_PAGES_DEFAULT   = 50      # only recover sites with at least this many pages
SITE_TIMEOUT        = 60.0    # hard ceiling per site

# ---------------------------------------------------------------------------
# Secrets / Firestore init
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
        print(f"  [recheck] could not load blueboot_secrets: {e}")
        return None


def _init_firestore(fb_key_dict):
    try:
        import firebase_admin
        from firebase_admin import firestore
        import firebase_admin.credentials as fb_creds
    except ImportError:
        raise RuntimeError("firebase-admin not installed — run: pip install firebase-admin")

    cred = (fb_creds.Certificate(fb_key_dict) if fb_key_dict
            else fb_creds.Certificate(os.getenv("FIREBASE_CREDENTIALS",
                                                "config/serviceAccountKey.json")))
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)

    db = firestore.client()
    return db


# ---------------------------------------------------------------------------
# Firestore scan (sync)
# ---------------------------------------------------------------------------

def _stream_excluded(
    db,
    countries:  list[str] | None,
    reason_filter: str | None,
    limit:      int | None,
    domains:    list[str] | None = None,
) -> list[tuple]:
    """Return [(doc_ref, doc_dict), ...] from sites_excluded."""
    from functions.utils import load_country_configs, tld_accepted_for  # noqa: PLC0415
    country_configs = load_country_configs()

    print(f"  [recheck] Scanning {EXCLUDED_COLLECTION}…")
    col = db.collection(EXCLUDED_COLLECTION)
    results: list[tuple] = []
    scanned = skipped = 0

    for doc in col.stream():
        scanned += 1
        data = doc.to_dict() or {}

        if countries:
            c = (data.get("country") or "").upper()
            if c not in countries:
                skipped += 1
                continue

        if domains:
            d = (data.get("domain") or "").lower().lstrip("www.")
            if not any(d == dom or d.endswith("." + dom) for dom in domains):
                skipped += 1
                continue

        if reason_filter:
            reason = (data.get("reason") or "").lower()
            if reason_filter.lower() not in reason:
                skipped += 1
                continue

        # Only re-check sites that were excluded with page_count == 0
        if int(data.get("page_count") or 0) != 0:
            skipped += 1
            continue

        website = data.get("website") or data.get("domain", "")
        if not website:
            skipped += 1
            continue

        # Skip sites whose TLD is not accepted for the stored country
        _d = (data.get("domain") or "").lower().lstrip("www.")
        _c = (data.get("country") or "NO").upper()
        if _d and not tld_accepted_for(_d, _c, country_configs):
            skipped += 1
            continue

        results.append((doc.reference, data))
        if limit and len(results) >= limit:
            break

    print(
        f"  [recheck] {scanned} scanned → {len(results)} to re-check  "
        f"({skipped} skipped by filter)"
    )
    return results


# ---------------------------------------------------------------------------
# Async recheck + write
# ---------------------------------------------------------------------------

async def _run_async(
    db,
    to_process:    list[tuple],
    concurrent:    int,
    min_pages:     int,
    dry_run:       bool,
    debug:         bool,
    load_configs,
    blocklist:     set,
    existing_leads: set,
) -> dict:
    try:
        import aiohttp as _aiohttp
    except ImportError:
        raise RuntimeError("aiohttp not installed — run: pip install aiohttp")

    from site_agent import process_site_async, upsert_site_lead  # noqa: PLC0415
    from functions.utils import is_blocked, tld_accepted_for  # noqa: PLC0415

    configs = load_configs()

    total       = len(to_process)
    sem         = asyncio.Semaphore(concurrent)
    loop        = asyncio.get_running_loop()
    counters    = {"total": total, "done": 0, "recovered": 0, "still_excluded": 0, "failed": 0, "blocked": 0, "duplicate": 0}

    connector       = _aiohttp.TCPConnector(ssl=False, limit=concurrent + 10)
    session_timeout = _aiohttp.ClientTimeout(total=45, connect=8)

    async def _recheck_one(session, doc_ref, data: dict) -> None:
        domain       = data.get("domain") or ""
        # Always scan from the root domain — the stored website may be a subpage URL
        # (e.g. https://example.com/page) which would cause robots.txt to be fetched
        # at /page/robots.txt and all sitemap candidates to resolve incorrectly.
        website      = f"https://{domain}" if domain else (data.get("website") or "")
        country      = (data.get("country") or "NO").upper()
        source_query = data.get("source_query", "")
        query_cat    = data.get("query_category", "")
        cfg          = configs.get(country, {})
        country_name = cfg.get("name", country)
        target_types = cfg.get("target_types", [])

        # Skip if already recovered into site_leads — always clean up the stale excluded doc
        if domain and domain.lower() in existing_leads:
            counters["duplicate"] += 1
            print(f"    [skip] already in site_leads — removing from excluded: {domain}")
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, lambda r=doc_ref: r.delete()),
                    timeout=12.0,
                )
            except Exception as exc:
                print(f"    [recheck] delete error (already-in-leads) {domain}: {exc}")
            return

        # Skip blocklisted domains
        if domain and is_blocked(domain, blocklist):
            counters["blocked"] += 1
            print(f"    [skip] blocklisted: {domain}")
            return

        # Skip domains whose TLD is not accepted for this country
        if domain and not tld_accepted_for(domain, country, configs):
            counters["blocked"] += 1
            print(f"    [skip] TLD not accepted for {country}: {domain}")
            return

        async with sem:
            try:
                lead, excl_reason = await asyncio.wait_for(
                    process_site_async(
                        session, website, source_query,
                        country, country_name,
                        min_pages=min_pages,
                        target_types=target_types,
                        query_category=query_cat,
                    ),
                    timeout=SITE_TIMEOUT,
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                counters["done"]   += 1
                counters["failed"] += 1
                print(f"    [{counters['done']}/{total}] TIMEOUT  {domain}")
                return
            except Exception as exc:
                counters["done"]   += 1
                counters["failed"] += 1
                print(f"    [{counters['done']}/{total}] ERROR  {domain}: {exc}")
                return

        counters["done"] += 1

        if lead is None or lead.page_count < min_pages:
            counters["still_excluded"] += 1
            reason = excl_reason if lead is None else f"page_count={lead.page_count if lead else 0}<{min_pages}"
            print(f"    [{counters['done']}/{total}] still excluded ({reason})  {domain}")
            return

        # Site now passes — save to site_leads and remove from sites_excluded
        counters["recovered"] += 1
        print(
            f"    [{counters['done']}/{total}] RECOVERED  {domain}"
            f"  pages={lead.page_count:,}  ({lead.sitemap_type})"
        )

        if not dry_run:
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, lambda l=lead: upsert_site_lead(l, LEADS_COLLECTION)),
                    timeout=12.0,
                )
            except Exception as exc:
                print(f"    [recheck] write error (site_leads) {domain}: {exc}")
                counters["failed"] += 1
                return
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, lambda r=doc_ref: r.delete()),
                    timeout=12.0,
                )
            except Exception as exc:
                print(f"    [recheck] delete error (sites_excluded) {domain}: {exc}")

    async def _safe_recheck_one(session, ref, data):
        domain = data.get("domain", "?")
        try:
            await _recheck_one(session, ref, data)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # Inner _recheck_one already handles these, but catch any that escape.
            counters["done"]   += 1
            counters["failed"] += 1
            print(f"    [recheck] HARD-TIMEOUT  {domain}")
        except Exception as exc:
            counters["done"]   += 1
            counters["failed"] += 1
            print(f"    [recheck] UNHANDLED-ERROR  {domain}: {exc}")

    async with _aiohttp.ClientSession(
        connector=connector, timeout=session_timeout
    ) as session:
        tasks = [
            asyncio.create_task(_safe_recheck_one(session, ref, data))
            for ref, data in to_process
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    return counters


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def recheck_excluded(
    countries:     list[str] | None = None,
    domains:       list[str] | None = None,
    reason_filter: str | None       = None,
    limit:         int | None       = None,
    concurrent:    int              = CONCURRENT_DEFAULT,
    min_pages:     int              = MIN_PAGES_DEFAULT,
    dry_run:       bool             = False,
    debug:         bool             = False,
) -> None:
    fb_key = _load_secrets()
    db     = _init_firestore(fb_key)

    to_proc = _stream_excluded(db, countries, reason_filter, limit, domains)
    if not to_proc:
        print("  [recheck] Nothing to re-check.")
        return

    print(f"\n  [recheck] Excluded collection : {EXCLUDED_COLLECTION}")
    print(f"  [recheck] Leads collection    : {LEADS_COLLECTION}")
    print(f"  [recheck] Concurrent          : {concurrent}")
    print(f"  [recheck] Min pages threshold : {min_pages}")
    print(f"  [recheck] Total to re-check   : {len(to_proc)}")
    print(f"  [recheck] Dry run             : {dry_run}\n")

    from site_agent import load_blocklist, preload_seen_domains  # noqa: PLC0415
    from functions.utils import load_country_configs  # noqa: PLC0415

    # Load these in sync context so they don't block the event loop inside asyncio.run()
    blocklist      = load_blocklist()
    existing_leads = preload_seen_domains(LEADS_COLLECTION)
    print(f"  [recheck] Blocklist patterns  : {len(blocklist)}")
    print(f"  [recheck] Existing leads      : {len(existing_leads)}")

    started  = datetime.now(timezone.utc)
    counters = asyncio.run(_run_async(
        db, to_proc, concurrent, min_pages, dry_run, debug,
        load_country_configs, blocklist, existing_leads,
    ))
    elapsed  = (datetime.now(timezone.utc) - started).total_seconds()

    print(f"\n  [recheck] Done in {elapsed:.0f}s")
    print(f"  Total checked    : {counters['total']}")
    print(f"  Recovered → leads: {counters['recovered']}")
    print(f"  Still excluded   : {counters['still_excluded']}")
    print(f"  Already in leads : {counters['duplicate']}")
    print(f"  Blocklisted      : {counters['blocked']}")
    print(f"  Failed/timeout   : {counters['failed']}")
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
        description="Re-check sites_excluded — recover sites that now pass the site check"
    )
    p.add_argument("--countries",  default=None, metavar="CODES",
                   help="Comma-separated country codes  e.g. NO,SE  (default: all)")
    p.add_argument("--domains",    default=None, metavar="NAMES",
                   help="Comma-separated domains to re-check  e.g. boligpluss.no")
    p.add_argument("--reason",     default=None, metavar="TEXT",
                   help="Only re-check sites whose exclusion reason contains this text")
    p.add_argument("--min-pages",  type=int, default=MIN_PAGES_DEFAULT, metavar="N",
                   help=f"Min page count to recover a site  (default: {MIN_PAGES_DEFAULT})")
    p.add_argument("--limit",      type=int, default=None, metavar="N",
                   help="Max sites to re-check")
    p.add_argument("--concurrent", type=int, default=CONCURRENT_DEFAULT, metavar="N",
                   help=f"Parallel fetches  (default: {CONCURRENT_DEFAULT})")
    p.add_argument("--dry-run",    action="store_true",
                   help="Print results without writing to Firestore")
    p.add_argument("--debug",      action="store_true",
                   help="Verbose sitemap fetch details")

    args = p.parse_args(argv)

    countries = None
    if args.countries:
        countries = [c.strip().upper() for c in args.countries.split(",") if c.strip()]

    domains = None
    if args.domains:
        domains = [d.strip().lower().lstrip("www.") for d in args.domains.split(",") if d.strip()]
    recheck_excluded(
        countries     = countries,
        domains       = domains,
        reason_filter = args.reason,
        limit         = args.limit,
        concurrent    = args.concurrent,
        min_pages     = args.min_pages,
        dry_run       = args.dry_run,
        debug         = args.debug,
    )


if __name__ == "__main__":
    main()
