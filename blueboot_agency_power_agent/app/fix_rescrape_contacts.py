"""fix_rescrape_contacts.py — re-scrape leads with mismatched phone/email data.

Finds all leads in Firestore where the phone count in `email_phones` exceeds
the email count in `emails` (the old pairing bug collected all phones from the
page and assigned them to the first email, dropping the extras when contacts
were written).  Also catches any contact doc that still has a comma in its
`phone` field.  Re-crawls each affected site with the improved scraper, then
replaces the stale contacts with fresh, correctly-paired name / phone / email
data.

What is updated on each lead document:
  emails, email_titles, email_phones, email_names, phones, crawled_at

What is NOT touched:
  reseller_score, priority, reasons, suggested_angle, categories, detected_tech
  (manually curated fields are preserved)

Usage:
    python app\\fix_rescrape_contacts.py --dry-run      ← find affected leads, no crawl
    python app\\fix_rescrape_contacts.py                ← live re-scrape + update
    python app\\fix_rescrape_contacts.py --country FI   ← one country only
    python app\\fix_rescrape_contacts.py --limit 20     ← cap at 20 leads

Options:
    --collection NAME   Firestore leads collection      (default: leads)
    --country CODE      Filter by country code(s), comma-separated
    --limit N           Max leads to re-scrape
    --workers N         Parallel crawl workers          (default: 50)
    --delay FLOAT       Seconds between page fetches    (default: 1.0)
    --max-pages N       Max pages crawled per site      (default: 4)
    --dry-run           List affected leads without crawling or writing
"""
from __future__ import annotations

import threading as _threading

# Guards firebase_admin.initialize_app against concurrent init
_local_fb_lock = _threading.Lock()
import asyncio
import importlib.util
import os
from datetime import datetime, timezone
from dataclasses import asdict
from pathlib import Path

import aiohttp

import _pathsetup  # noqa: F401

# ---------------------------------------------------------------------------
# Load async crawl function from collect-functions/search_runner.py
# (hyphenated directory name — must use importlib)
# ---------------------------------------------------------------------------

def _load_search_runner():
    sr_path = Path(__file__).parent / "collect-functions" / "search_runner.py"
    spec    = importlib.util.spec_from_file_location("search_runner", sr_path)
    mod     = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_sr = _load_search_runner()
_async_crawl_site = _sr._async_crawl_site

# ---------------------------------------------------------------------------
# Firebase helpers
# ---------------------------------------------------------------------------

def _get_db(collection: str | None = None):
    try:
        import firebase_admin
        from firebase_admin import firestore
        import firebase_admin.credentials as fb_creds
    except ImportError:
        print("[rescrape] firebase-admin not installed")
        return None, None, None

    secrets_path = Path(__file__).parent.parent / "blueboot_secrets.py"
    key_dict = None
    if secrets_path.exists():
        try:
            spec = importlib.util.spec_from_file_location("blueboot_secrets", secrets_path)
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            key_dict = getattr(mod, "fireBaseAdminKey", None)
        except Exception as e:
            print(f"[rescrape] could not load blueboot_secrets: {e}")

    cred = (fb_creds.Certificate(key_dict) if key_dict
            else fb_creds.Certificate(os.getenv("FIREBASE_CREDENTIALS",
                                                "config/serviceAccountKey.json")))
    col_name = collection or os.getenv("FIRESTORE_COLLECTION", "leads")
    with _local_fb_lock:
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
    db  = firestore.client()
    col = db.collection(col_name)
    return db, col, col_name


# ---------------------------------------------------------------------------
# Step 1 — find leads with mismatched phone / email counts
# ---------------------------------------------------------------------------


def find_affected_leads(
    db, col, col_name: str,
    country_filter: set[str] | None,
    limit: int | None,
) -> list[dict]:
    """Return all leads (optionally filtered by country/limit) for re-scraping."""

    print("[rescrape] Loading all leads…")
    leads_to_fix: list[dict] = []
    for ldoc in col.stream():
        d = ldoc.to_dict()
        if not d:
            continue
        country = (d.get("country") or "").strip().upper()
        if country_filter and country not in country_filter:
            continue
        d["_lid"] = ldoc.id
        leads_to_fix.append(d)
        if limit and len(leads_to_fix) >= limit:
            break

    print(f"[rescrape] {len(leads_to_fix)} lead(s) to re-scrape "
          f"(after country/limit filter).")
    return leads_to_fix


# ---------------------------------------------------------------------------
# Step 2 — async re-crawl worker
# ---------------------------------------------------------------------------

async def _recrawl_one(
    session:   aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    lead:      dict,
    configs:   dict,
    max_pages: int,
    delay:     float,
    results:   list,     # shared: append (lid, Lead) on success
    counters:  dict,
) -> None:
    async with semaphore:
        lid      = lead["_lid"]
        website  = (lead.get("website") or lead.get("domain") or "").strip()
        country  = (lead.get("country") or "").strip().upper()
        sq       = lead.get("source_query", "rescrape")

        if not website:
            counters["skipped"] += 1
            return

        cfg = configs.get(country, {})

        try:
            fresh = await _async_crawl_site(
                session, website, sq,
                max_pages=max_pages,
                delay=delay,
                country_code=country,
                country_cfg=cfg,
                min_score=0,          # don't filter already-stored leads by score
                source=lead.get("found_by_catalog") == "yes" and "catalog" or "search",
            )
        except Exception as exc:
            print(f"  [!] {website}: {exc}")
            counters["errors"] += 1
            return

        n = counters["done"] + 1
        counters["done"] = n

        if fresh is None or not fresh.emails:
            print(f"  [{n}] {website} — no emails found, skipping update")
            counters["no_email"] += 1
            return

        print(f"  [{n}] {website}")
        emails = [e.strip() for e in fresh.emails.split(",") if e.strip()]
        phones = [p.strip() for p in fresh.email_phones.split(",") if True] if fresh.email_phones else []
        names  = [nm.strip() for nm in fresh.email_names.split(",") if True] if fresh.email_names else []
        for i, em in enumerate(emails):
            ph = phones[i] if i < len(phones) else ""
            nm = names[i]  if i < len(names)  else ""
            print(f"        {em:<40}  name={nm!r:25}  phone={ph!r}")

        results.append((lid, fresh))
        counters["updated"] += 1


# ---------------------------------------------------------------------------
# Step 3 — write fresh contacts to Firestore
# ---------------------------------------------------------------------------

def _write_fresh_contacts(db, col, lid: str, fresh, dry_run: bool) -> int:
    """Delete old contacts and write new ones. Returns number of contacts written."""
    from app.functions.firebase_sync import _contact_id
    try:
        from app.functions.firebase_sync import _contact_id
    except ImportError:
        import hashlib
        def _contact_id(email: str) -> str:
            return hashlib.sha1(email.lower().encode()).hexdigest()[:10]

    lead_ref     = col.document(lid)
    contacts_col = lead_ref.collection("contacts")

    emails     = [e.strip() for e in fresh.emails.split(",")       if e.strip()] if fresh.emails       else []
    titles     = [t.strip() for t in fresh.email_titles.split(",") if True]      if fresh.email_titles else []
    per_phones = [p.strip() for p in fresh.email_phones.split(",") if True]      if fresh.email_phones else []
    per_names  = [n.strip() for n in fresh.email_names.split(",")  if True]      if fresh.email_names  else []

    # Build a set of existing contact IDs to distinguish create vs update
    existing_cids = {cdoc.id for cdoc in contacts_col.stream()}

    written = 0
    for i, email in enumerate(emails):
        cid   = _contact_id(email)
        phone = per_phones[i] if i < len(per_phones) else ""
        name  = per_names[i]  if i < len(per_names)  else ""
        title = titles[i]     if i < len(titles)      else ""

        if cid in existing_cids:
            # Existing contact — merge only name/phone/title; preserve everything else
            data = {"phone": phone, "name": name, "title": title}
        else:
            # New contact — write full record
            data = {
                "email":        email,
                "name":         name,
                "title":        title,
                "phone":        phone,
                "lead_id":      lid,
                "company":      fresh.company,
                "domain":       fresh.domain,
                "website":      fresh.website,
                "country":      fresh.country,
                "country_name": fresh.country_name,
                "linkedin":     fresh.linkedin,
            }

        if not dry_run:
            contacts_col.document(cid).set(data, merge=True)
        written += 1

    return written


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _find_leads_by_urls(col, urls: list[str]) -> list[dict]:
    """Look up Firestore lead docs matching any of the given URLs/domains."""
    from tldextract import extract as tld_extract

    def _domain(u: str) -> str:
        ext = tld_extract(u)
        return f"{ext.domain}.{ext.suffix}".lower() if ext.suffix else u.lower()

    target_domains = {_domain(u) for u in urls}
    found: list[dict] = []

    for ldoc in col.stream():
        d = ldoc.to_dict() or {}
        site = d.get("website") or d.get("domain") or ""
        if _domain(site) in target_domains:
            d["_lid"] = ldoc.id
            found.append(d)

    return found


def fix_rescrape_contacts(
    collection: str | None  = None,
    countries:  list[str] | None = None,
    urls:       list[str] | None = None,
    limit:      int | None  = None,
    workers:    int         = 50,
    delay:      float       = 1.0,
    max_pages:  int         = 4,
    dry_run:    bool        = False,
) -> None:
    try:
        from app.functions.utils import load_country_configs
    except ImportError:
        from functions.utils import load_country_configs

    db, col, col_name = _get_db(collection)
    if col is None:
        raise RuntimeError("Could not connect to Firestore.")

    tag = " [DRY RUN]" if dry_run else ""
    print(f"[rescrape] Collection : {col_name}{tag}")
    print(f"[rescrape] Workers    : {workers}  delay={delay}s  max-pages={max_pages}")

    # Build country filter — accept ISO code OR full name
    country_filter: set[str] | None = None
    if countries:
        configs_raw = load_country_configs()
        country_filter = set()
        for c_raw in countries:
            c_up = c_raw.strip().upper()
            country_filter.add(c_up)
            for code, cfg in configs_raw.items():
                if code.upper() == c_up and isinstance(cfg, dict) and cfg.get("name"):
                    country_filter.add(cfg["name"].upper())

    # ------------------------------------------------------------------
    # Step 1: find affected leads
    # ------------------------------------------------------------------
    if urls:
        # --url mode: bypass detection, target specific sites directly
        print(f"[rescrape] --url mode: looking up {len(urls)} URL(s) in Firestore…")
        leads_to_fix = _find_leads_by_urls(col, urls)
        if not leads_to_fix:
            # URL not in Firestore yet — build a minimal stub so we still crawl it
            print("[rescrape] No matching lead doc found — will crawl and print results only.")
            leads_to_fix = [
                {"_lid": None, "website": u, "domain": u,
                 "country": (countries[0] if countries else ""),
                 "source_query": "manual"}
                for u in urls
            ]
        print(f"[rescrape] {len(leads_to_fix)} lead(s) targeted.")
    else:
        leads_to_fix = find_affected_leads(db, col, col_name, country_filter, limit)

    if not leads_to_fix:
        print("[rescrape] Nothing to do.")
        return

    if dry_run:
        print(f"\n[rescrape] DRY RUN — would re-scrape {len(leads_to_fix)} leads:")
        for lead in leads_to_fix:
            print(f"  {lead.get('website', lead.get('domain', lead['_lid']))}"
                  f"  ({lead.get('country', '')})")
        return

    # ------------------------------------------------------------------
    # Step 2: async re-crawl
    # ------------------------------------------------------------------
    configs  = load_country_configs()
    results: list  = []   # (lid, Lead)
    counters: dict = {"done": 0, "updated": 0, "no_email": 0, "errors": 0, "skipped": 0}

    async def _run_all():
        semaphore = asyncio.Semaphore(workers)
        connector = aiohttp.TCPConnector(limit=workers, limit_per_host=5, ssl=False)
        timeout   = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async def _recrawl_one_guarded(lead):
                # RULE 2: chained-await crawl needs a hard top-level ceiling,
                # otherwise one stalled site freezes asyncio.gather forever.
                try:
                    await asyncio.wait_for(
                        _recrawl_one(session, semaphore, lead, configs,
                                     max_pages, delay, results, counters),
                        timeout=120.0,
                    )
                except asyncio.TimeoutError:
                    counters["errors"] += 1
                    print(f"  [rescrape] timeout (>120s) on {lead.get('website') or lead.get('domain')}")
            tasks = [_recrawl_one_guarded(lead) for lead in leads_to_fix]
            await asyncio.gather(*tasks, return_exceptions=True)

    print(f"\n[rescrape] Crawling {len(leads_to_fix)} sites…\n")
    asyncio.run(_run_all())

    # ------------------------------------------------------------------
    # Step 3: write fresh contacts to Firestore
    # ------------------------------------------------------------------
    if not results:
        print("\n[rescrape] No leads returned fresh contact data.")
    else:
        print(f"\n[rescrape] Writing {len(results)} updated leads to Firestore…")
        try:
            from app.functions.firebase_sync import _lead_id
        except ImportError:
            from functions.firebase_sync import _lead_id

        PROGRESS_EVERY = 10
        total_contacts = 0
        for i, (lid, fresh) in enumerate(results, 1):
            if lid is None:
                # --url stub: no existing doc found — compute deterministic ID
                # and merge the lead doc (preserves any existing score/priority/etc.)
                lid = _lead_id(fresh.website)
                from dataclasses import asdict as _asdict
                lead_dict = _asdict(fresh)
                lead_dict["lead_id"] = lid
                col.document(lid).set(lead_dict, merge=True)
                print(f"  [rescrape] Merged lead doc {lid} for {fresh.website}")

            written = _write_fresh_contacts(db, col, lid, fresh, dry_run=False)
            total_contacts += written
            if i % PROGRESS_EVERY == 0:
                print(f"  [rescrape] {i}/{len(results)} leads written…")
        print(f"  [rescrape] Done — {len(results)} leads, {total_contacts} contacts written.")

    print(f"\n[rescrape] Summary:")
    print(f"  Affected leads found  : {len(leads_to_fix)}")
    print(f"  Successfully re-crawled: {counters['updated']}")
    print(f"  No emails found       : {counters['no_email']}")
    print(f"  Crawl errors          : {counters['errors']}")
    print(f"  Skipped (no URL)      : {counters['skipped']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(
        description="Re-scrape leads whose contacts have comma-separated phone lists."
    )
    p.add_argument("--collection", metavar="NAME", default=None,
                   help="Firestore leads collection (default: leads)")
    p.add_argument("--country",    metavar="CODE", action="append", dest="countries",
                   help="Country code(s) to filter (repeatable or comma-separated)")
    p.add_argument("--limit",      metavar="N", type=int, default=None,
                   help="Max number of leads to re-scrape")
    p.add_argument("--workers",    metavar="N", type=int, default=50,
                   help="Parallel crawl workers (default: 50)")
    p.add_argument("--delay",      metavar="SECS", type=float, default=1.0,
                   help="Seconds between page fetches per worker (default: 1.0)")
    p.add_argument("--max-pages",  metavar="N", type=int, default=4,
                   help="Max pages to crawl per site (default: 4)")
    p.add_argument("--url",        metavar="URL", action="append", dest="urls",
                   help="Force re-scrape of a specific URL (bypasses detection; repeatable)")
    p.add_argument("--dry-run",    action="store_true",
                   help="List affected leads without crawling or writing")
    args = p.parse_args(argv)

    countries = None
    if args.countries:
        expanded = []
        for c in args.countries:
            expanded.extend(x.strip().upper() for x in c.split(",") if x.strip())
        countries = expanded or None

    fix_rescrape_contacts(
        collection=args.collection,
        countries=countries,
        urls=args.urls or None,
        limit=args.limit,
        workers=args.workers,
        delay=args.delay,
        max_pages=args.max_pages,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
