"""site_enrich_agent.py -- Enrich site_leads documents with AI classification.

Reads site_leads from Firestore, sends batches concurrently to OpenAI for
classification and keyword enrichment, then writes results back asynchronously.

Pipeline:
  1. Firestore stream (sync) -- collect unclassified site_leads
  2. Split into batches of --batch-size  (default 15)
  3. Launch all batches as async tasks, capped by --concurrent semaphore (default 3)
  4. Each task: async OpenAI call -> parse JSON -> async Firestore write (executor)
  5. Summary printed when all tasks complete

Fields written to each site_leads doc:
  ai_sector         -- e.g. "manufacturing", "technology", "public_sector"
  ai_company_type   -- e.g. "B2B", "government", "media"
  ai_country        -- ISO 3166-1 alpha-2 code inferred from site content, e.g. "NO"
  ai_keywords       -- validated + enriched keyword list (max 25)
  ai_summary        -- one-sentence description of the site
  ai_confidence     -- 0.0-1.0 score
  ai_classified_at  -- ISO timestamp
  keywords          -- existing keywords merged with ai_keywords (deduped, max 25)

Fields written when --update-sitemaps is used:
  sitemap_url       -- canonical sitemap URL found for the site
  sitemap_type      -- "index" | "urlset" | "none"
  page_count        -- estimated total indexed pages (updated)

Usage:
    python app/site_enrich_agent.py
    python app/site_enrich_agent.py --countries NO,SE --batch-size 20
    python app/site_enrich_agent.py --limit 50 --dry-run
    python app/site_enrich_agent.py --force --concurrent 5
    python app/site_enrich_agent.py --update-sitemaps --skip-ai --countries NO
    python app/site_enrich_agent.py --update-sitemaps --force-sitemaps
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import _pathsetup  # noqa: F401

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OPENAI_MODEL       = "gpt-5.4-nano"
BATCH_SIZE         = 15    # sites per OpenAI call
CONCURRENT_BATCHES = 3     # max simultaneous OpenAI calls
RETRY_ATTEMPTS     = 3
RETRY_DELAY        = 6.0   # seconds to wait on rate-limit

COLLECTION_DEFAULT = "site_leads"

SECTORS = [
    "manufacturing", "technology", "consulting", "public_sector",
    "healthcare", "education", "media", "ecommerce", "finance",
    "energy", "logistics", "food", "real_estate", "association",
    "legal", "construction", "agriculture", "tourism", "other",
]

COMPANY_TYPES = ["B2B", "B2C", "government", "NGO", "media", "education", "mixed"]

_SYSTEM_PROMPT = (
    "You are a B2B lead classifier. You receive a list of websites and must return "
    "a JSON array classifying each one.\n\n"
    "For each site return an object with exactly these keys:\n"
    '  "lead_id"      : same string as in the input — never change it\n'
    '  "sector"       : one of ' + json.dumps(SECTORS) + "\n"
    '  "company_type" : one of ' + json.dumps(COMPANY_TYPES) + "\n"
    '  "country"      : ISO 3166-1 alpha-2 code (e.g. "NO", "SE", "DE") for the country '
    "where the company or organisation is located. Infer from the URL TLD, language, "
    "address mentions, phone prefixes, or other signals in the title/description/keywords. "
    'Use the input "country" field as a strong hint but correct it if clearly wrong. '
    'Return "" if genuinely impossible to determine.\n'
    '  "keywords"     : array of up to 25 lowercase English keywords relevant to '
    "this site (merge and clean the input keywords, add obvious missing ones, "
    "remove noise/stopwords)\n"
    '  "summary"      : one sentence (max 20 words) describing what the site does\n'
    '  "confidence"   : float 0.0-1.0 reflecting how certain you are\n\n'
    "Return ONLY a valid JSON array — no markdown, no explanation, no extra keys."
)


def _user_prompt(batch: list[dict]) -> str:
    items = [
        {
            "lead_id":      site["lead_id"],
            "url":          site.get("website", ""),
            "title":        site.get("title", "")[:120],
            "description":  site.get("description", "")[:200],
            "keywords":     site.get("keywords", [])[:30],
            "target_types": site.get("target_types", []),
            "country":      site.get("country", ""),
        }
        for site in batch
    ]
    return json.dumps(items, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Secrets helpers (sync — called once at startup)
# ---------------------------------------------------------------------------

def _load_secrets():
    """Return (openai_api_key, firebase_key_dict) from blueboot_secrets.py."""
    secrets_path = Path(__file__).parent.parent / "blueboot_secrets.py"
    if not secrets_path.exists():
        return None, None
    try:
        spec = importlib.util.spec_from_file_location("blueboot_secrets", secrets_path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cfg     = getattr(mod, "openAiConfig", {})
        api_key = cfg.get("defaultProjectKey")
        fb_key  = getattr(mod, "fireBaseAdminKey", None)
        return api_key, fb_key
    except Exception as e:
        print(f"  [enrich] could not load blueboot_secrets: {e}")
        return None, None


def _init_openai_async(api_key: str):
    try:
        from openai import AsyncOpenAI
    except ImportError:
        raise RuntimeError("openai package not installed — run: pip install openai")
    return AsyncOpenAI(api_key=api_key)


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
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)

    db  = firestore.client()
    col = db.collection(collection)
    return db, col


# ---------------------------------------------------------------------------
# Firestore scan (sync -- done once before async loop starts)
# ---------------------------------------------------------------------------

def _stream_unclassified(
    col,
    countries: list[str] | None,
    force:     bool,
    limit:     int | None,
) -> list[tuple]:
    """Return [(doc_ref, doc_dict), ...] for leads that need classification."""
    print("  [enrich] Scanning site_leads…")
    results: list[tuple] = []
    scanned = skipped_done = skipped_country = 0

    for doc in col.stream():
        scanned += 1
        data = doc.to_dict() or {}

        if countries:
            c = (data.get("country") or "").upper()
            if c not in countries and c != "*":
                skipped_country += 1
                continue

        if not force and data.get("ai_classified_at"):
            skipped_done += 1
            continue

        results.append((doc.reference, data))
        if limit and len(results) >= limit:
            break

    print(
        f"  [enrich] {scanned} scanned → {len(results)} to classify  "
        f"(skipped: {skipped_done} already done, {skipped_country} wrong country)"
    )
    return results


# ---------------------------------------------------------------------------
# Sitemap update pass
# ---------------------------------------------------------------------------

SITEMAP_CONCURRENT = 10    # parallel sitemap fetches
SITEMAP_TIMEOUT    = 120.0  # hard ceiling per site (robots.txt + index + url-sets)


def _stream_for_sitemaps(
    col,
    countries:      list[str] | None,
    force_sitemaps: bool,
    limit:          int | None,
) -> list[tuple]:
    """Return [(doc_ref, doc_dict), ...] for leads that need sitemap data."""
    print("  [sitemaps] Scanning site_leads…")
    results: list[tuple] = []
    scanned = skipped = 0

    for doc in col.stream():
        scanned += 1
        data = doc.to_dict() or {}

        if countries:
            c = (data.get("country") or "").upper()
            if c not in countries and c != "*":
                continue

        if not force_sitemaps:
            s_url  = data.get("sitemap_url", "")
            s_type = data.get("sitemap_type", "")
            if s_url and s_type and s_type != "none":
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
        f"  [sitemaps] {scanned} scanned → {len(results)} need sitemap update  "
        f"({skipped} skipped — already have sitemap)"
    )
    return results


async def _run_sitemaps_async(
    to_process: list[tuple],
    concurrent: int,
    dry_run:    bool,
) -> dict:
    """Fetch sitemaps for all leads, write back sitemap_url/type/page_count."""
    try:
        import aiohttp as _aiohttp
    except ImportError:
        raise RuntimeError("aiohttp not installed — run: pip install aiohttp")
    from site_agent import read_sitemap_async  # noqa: PLC0415

    total    = len(to_process)
    sem      = asyncio.Semaphore(concurrent)
    loop     = asyncio.get_running_loop()
    counters = {"total": total, "done": 0, "updated": 0, "failed": 0}

    connector      = _aiohttp.TCPConnector(ssl=False, limit=concurrent + 5)
    session_timeout = _aiohttp.ClientTimeout(total=45, connect=8)

    async def _fetch_one(session, doc_ref, data: dict) -> None:
        website = data.get("website") or f"https://{data.get('domain', '')}"
        domain  = data.get("domain", website)
        async with sem:
            try:
                count, s_url, s_type, s_sitemaps, s_oldest, s_newest, s_platform = await asyncio.wait_for(
                    read_sitemap_async(session, website),
                    timeout=SITEMAP_TIMEOUT,
                )
            except Exception as exc:
                counters["done"]   += 1
                counters["failed"] += 1
                print(f"    [{counters['done']}/{total}] ERR  {domain}: {exc}")
                return

        updates = {
            "sitemap_url":         s_url,
            "sitemap_type":        s_type,
            "page_count":          count,
            "sitemaps":        s_sitemaps,
            "sitemap_oldest_date": s_oldest,
            "sitemap_newest_date": s_newest,
            "platform":            s_platform,
        }
        counters["done"] += 1
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
                print(f"    [sitemaps] write error {domain}: {exc}")
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
        await asyncio.gather(*tasks)

    return counters


# ---------------------------------------------------------------------------
# Async OpenAI call with retry
# ---------------------------------------------------------------------------

async def _classify_batch_async(
    client:     object,
    semaphore:  asyncio.Semaphore,
    batch_data: list[dict],
    batch_num:  int,
    batch_tot:  int,
) -> list[dict]:
    """Call OpenAI for one batch with retry. Semaphore is acquired per-attempt
    and released before any sleep, so rate-limit waits never block other batches."""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        sleep_secs = 0.0
        result     = None

        async with semaphore:
            print(f"  [enrich] batch {batch_num}/{batch_tot}  ({len(batch_data)} sites) → OpenAI…")
            try:
                response = await client.chat.completions.create(
                    model=OPENAI_MODEL,
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user",   "content": _user_prompt(batch_data)},
                    ],
                )
                raw    = response.choices[0].message.content or ""
                parsed = json.loads(raw)
                # Model may wrap the array in a dict key e.g. {"results": [...]}
                if isinstance(parsed, dict):
                    parsed = next(
                        (v for v in parsed.values() if isinstance(v, list)), []
                    )
                if isinstance(parsed, list):
                    print(f"  [enrich] batch {batch_num}/{batch_tot}  ✓ {len(parsed)} results")
                    return parsed
                print(f"  [enrich] batch {batch_num} unexpected shape: {type(parsed)}")
                return []

            except json.JSONDecodeError as e:
                print(f"  [enrich] batch {batch_num} JSON error (attempt {attempt}): {e}")
                sleep_secs = RETRY_DELAY
            except Exception as e:
                err = str(e)
                print(f"  [enrich] batch {batch_num} OpenAI error (attempt {attempt}): {err}")
                if "rate_limit" in err.lower() or "429" in err:
                    sleep_secs = RETRY_DELAY * attempt
                    print(f"  [enrich] rate limit — sleeping {sleep_secs}s after semaphore release…")
                else:
                    sleep_secs = RETRY_DELAY

        # Sleep OUTSIDE the semaphore so other batches can proceed
        if sleep_secs and attempt < RETRY_ATTEMPTS:
            await asyncio.sleep(sleep_secs)

    print(f"  [enrich] batch {batch_num} failed after {RETRY_ATTEMPTS} attempts")
    return []


# ---------------------------------------------------------------------------
# Async batch processor (classify + write)
# ---------------------------------------------------------------------------

async def _process_batch_async(
    client:     object,
    semaphore:  asyncio.Semaphore,
    loop:       asyncio.AbstractEventLoop,
    batch_ids:  list[str],
    ref_map:    dict,
    batch_num:  int,
    batch_tot:  int,
    now_ts:     str,
    counters:   dict,
    dry_run:    bool,
) -> None:
    batch_data = [ref_map[lid][1] | {"lead_id": lid} for lid in batch_ids]
    results    = await _classify_batch_async(client, semaphore, batch_data, batch_num, batch_tot)

    result_map: dict[str, dict] = {r["lead_id"]: r for r in results if r.get("lead_id")}

    for lid in batch_ids:
        r    = result_map.get(lid)
        orig = ref_map[lid][1]

        if not r:
            print(f"    NO RESULT  {orig.get('domain', lid)}")
            counters["failed"] += 1
            counters["done"]   += 1
            continue

        existing_kw = orig.get("keywords") or []
        new_kw      = r.get("keywords") or []
        merged_kw   = list(dict.fromkeys(existing_kw + new_kw))[:25]

        ai_country = (r.get("country") or "").strip().upper()[:2]

        updates = {
            "ai_sector":        r.get("sector", "other"),
            "ai_company_type":  r.get("company_type", ""),
            "ai_country":       ai_country,
            "ai_keywords":      new_kw[:25],
            "ai_summary":       r.get("summary", ""),
            "ai_confidence":    float(r.get("confidence", 0.0)),
            "ai_classified_at": now_ts,
            "keywords":         merged_kw,
        }

        total   = counters["total"]
        done    = counters["done"] + 1
        domain  = orig.get("domain", lid)
        sector  = updates["ai_sector"]
        ctype   = updates["ai_company_type"]
        country = updates["ai_country"] or "??"
        conf    = updates["ai_confidence"]
        smry    = (updates["ai_summary"] or "")[:65]
        print(f"    [{done}/{total}] {domain:<42} {sector} / {ctype}  {country}  conf={conf:.2f}")
        if smry:
            print(f"           {smry}")

        counters["done"] += 1

        if not dry_run:
            doc_ref = ref_map[lid][0]
            try:
                await loop.run_in_executor(
                    None, lambda ref=doc_ref, upd=updates: ref.set(upd, merge=True)
                )
                counters["updated"] += 1
            except Exception as e:
                print(f"    [enrich] write error for {lid}: {e}")
                counters["failed"] += 1
        else:
            counters["updated"] += 1


# ---------------------------------------------------------------------------
# Async entry point
# ---------------------------------------------------------------------------

async def _run_async(
    client:     object,
    to_process: list[tuple],
    batch_size: int,
    concurrent: int,
    dry_run:    bool,
) -> dict:
    total    = len(to_process)
    now_ts   = datetime.now(timezone.utc).isoformat(timespec="seconds")
    semaphore = asyncio.Semaphore(concurrent)
    loop      = asyncio.get_running_loop()
    counters  = {"total": total, "done": 0, "updated": 0, "failed": 0}

    ref_map  = {(data.get("lead_id") or doc.id): (doc, data)
                for doc, data in to_process}
    id_order = [(data.get("lead_id") or doc.id) for doc, data in to_process]

    b_tot  = (total + batch_size - 1) // batch_size
    tasks  = []
    for i, start in enumerate(range(0, total, batch_size), start=1):
        batch_ids = id_order[start : start + batch_size]
        tasks.append(
            asyncio.create_task(
                _process_batch_async(
                    client, semaphore, loop,
                    batch_ids, ref_map,
                    i, b_tot, now_ts, counters, dry_run,
                )
            )
        )

    await asyncio.gather(*tasks)
    return counters


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def enrich_site_leads(
    collection:     str           = COLLECTION_DEFAULT,
    countries:      list[str] | None = None,
    limit:          int | None    = None,
    batch_size:     int           = BATCH_SIZE,
    concurrent:     int           = CONCURRENT_BATCHES,
    force:          bool          = False,
    dry_run:        bool          = False,
    update_sitemaps: bool         = False,
    skip_ai:        bool          = False,
    force_sitemaps: bool          = False,
) -> None:
    api_key, fb_key = _load_secrets()
    _, col  = _init_firestore(fb_key, collection)

    # ── sitemap pass (optional) ──────────────────────────────────────────────
    if update_sitemaps:
        sm_proc = _stream_for_sitemaps(col, countries, force_sitemaps, limit)
        if sm_proc:
            print(f"\n  [sitemaps] Fetching sitemaps for {len(sm_proc)} lead(s)…")
            sm_counters = asyncio.run(
                _run_sitemaps_async(sm_proc, SITEMAP_CONCURRENT, dry_run)
            )
            print("\n  [sitemaps] Done.")
            print(f"  Updated  : {sm_counters['updated']}")
            print(f"  Failed   : {sm_counters['failed']}")
            if dry_run:
                print("  (dry-run — nothing written to Firestore)")
        else:
            print("  [sitemaps] Nothing to update.")

    if skip_ai:
        return

    # OpenAI key only needed for AI classification
    api_key = api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "No OpenAI API key found. Set OPENAI_API_KEY or add defaultProjectKey "
            "to blueboot_secrets.py openAiConfig."
        )

    client  = _init_openai_async(api_key)
    to_proc = _stream_unclassified(col, countries, force, limit)

    if not to_proc:
        print("  [enrich] Nothing to do.")
        return

    total = len(to_proc)
    b_tot = (total + batch_size - 1) // batch_size
    print(f"\n  [enrich] Model       : {OPENAI_MODEL}")
    print(f"  [enrich] Batch size  : {batch_size}  ({b_tot} batches)")
    print(f"  [enrich] Concurrent  : {concurrent} parallel OpenAI calls")
    print(f"  [enrich] Total leads : {total}")
    print(f"  [enrich] Dry run     : {dry_run}\n")

    counters = asyncio.run(
        _run_async(client, to_proc, batch_size, concurrent, dry_run)
    )

    print("\n  [enrich] Done.")
    print(f"  Total    : {counters['total']}")
    print(f"  Updated  : {counters['updated']}")
    print(f"  Failed   : {counters['failed']}")
    if dry_run:
        print("  (dry-run — nothing written to Firestore)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> None:
    from dotenv import load_dotenv
    load_dotenv()

    p = argparse.ArgumentParser(
        description="Site Enrich Agent -- classify site_leads with OpenAI (async)"
    )
    p.add_argument("--collection",  default=COLLECTION_DEFAULT, metavar="NAME",
                   help=f"Firestore collection  (default: {COLLECTION_DEFAULT})")
    p.add_argument("--countries",   default=None, metavar="CODES",
                   help="Comma-separated country codes  e.g. NO,SE")
    p.add_argument("--limit",       type=int, default=None, metavar="N",
                   help="Max leads to process")
    p.add_argument("--batch-size",  type=int, default=BATCH_SIZE, metavar="N",
                   help=f"Sites per OpenAI call  (default: {BATCH_SIZE})")
    p.add_argument("--concurrent",  type=int, default=CONCURRENT_BATCHES, metavar="N",
                   help=f"Parallel OpenAI calls  (default: {CONCURRENT_BATCHES})")
    p.add_argument("--force",       action="store_true",
                   help="Re-classify leads that are already classified")
    p.add_argument("--dry-run",     action="store_true",
                   help="Print results without writing to Firestore")
    p.add_argument("--update-sitemaps", action="store_true",
                   help="Fetch and update sitemap_url/sitemap_type/page_count for leads missing them")
    p.add_argument("--skip-ai",     action="store_true",
                   help="Skip OpenAI classification (use with --update-sitemaps for sitemap-only run)")
    p.add_argument("--force-sitemaps", action="store_true",
                   help="Re-fetch sitemaps even for leads that already have sitemap_url set")

    args = p.parse_args(argv)

    countries = None
    if args.countries:
        countries = [c.strip().upper() for c in args.countries.split(",") if c.strip()]

    enrich_site_leads(
        collection      = args.collection,
        countries       = countries,
        limit           = args.limit,
        batch_size      = args.batch_size,
        concurrent      = args.concurrent,
        force           = args.force,
        dry_run         = args.dry_run,
        update_sitemaps = args.update_sitemaps,
        skip_ai         = args.skip_ai,
        force_sitemaps  = args.force_sitemaps,
    )


if __name__ == "__main__":
    main()
