"""site_location_enrich.py — Enrich site_leads with AI-inferred company location.

Reads site_leads that have no `location` field (or --force to re-enrich all),
asks OpenAI in batches of 50 to infer the city/region, then writes results back.

Fields written to each site_leads doc:
  location            -- "City, Country"  e.g. "London, UK" or "Pune, India"
  location_city       -- city only        e.g. "London"
  location_region     -- state/region     e.g. "England" (optional)
  location_country    -- country name     e.g. "United Kingdom"
  location_confidence -- 0.0-1.0  how confident the model is
  location_source     -- what signals were used: "domain", "content", "address", "inferred"
  location_enriched_at -- ISO timestamp

Usage:
    python app/site_location_enrich.py --countries UK
    python app/site_location_enrich.py --countries IN --dry-run 20
    python app/site_location_enrich.py --countries UK IN --batch-size 50 --concurrent 4
    python app/site_location_enrich.py --force --countries NO
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

OPENAI_MODEL       = "gpt-5.4-mini"
BATCH_SIZE         = 50     # sites per OpenAI call
CONCURRENT_BATCHES = 3      # max simultaneous OpenAI calls
RETRY_ATTEMPTS     = 3
RETRY_DELAY        = 6.0
COLLECTION_DEFAULT = "site_leads"

_SYSTEM_PROMPT = """\
You are a company location classifier. Given a list of websites, infer the precise
physical location (city and country) of each company.

PRIORITY ORDER — use the strongest signal available:
  1. Full address on the site (street, city, postcode) — highest confidence
  2. Phone number with area code (020 → London, 0161 → Manchester, 040 → Mumbai, 020 → Delhi)
  3. Postcode / PIN visible in summary (SW1A → London, 411001 → Pune, 400001 → Mumbai)
  4. City name explicitly mentioned in summary or keywords
  5. Company name containing a city (e.g. "Manchester Digital", "Pune Web Solutions")
  6. Regional language or spelling clues (e.g. "colour"/"neighbourhood" → UK)
  7. Domain TLD alone — lowest confidence, country only, no city

CITY DETECTION — be specific, not generic:
  - Always identify the specific city when any signal supports it.
  - UK: distinguish London, Manchester, Birmingham, Leeds, Edinburgh, Bristol, Sheffield, Liverpool, Cardiff, Glasgow.
  - India: distinguish Pune, Mumbai, Bangalore, Delhi, Chennai, Hyderabad, Ahmedabad, Kolkata, Jaipur, Surat.
  - Norway: Oslo, Bergen, Trondheim, Stavanger, Kristiansand.
  - Sweden: Stockholm, Gothenburg, Malmö, Uppsala, Linköping.
  - Denmark: Copenhagen, Aarhus, Odense, Aalborg.
  - NEVER default to the capital city just because you know the country — only set
    location_city if you have an actual signal pointing to a specific city.

Return a JSON array — one object per site — with exactly these keys:
  "lead_id"            : same string as in the input, never change it
  "location_full"      : human-readable full location string, e.g. "London, England, United Kingdom"
                         or "Pune, Maharashtra, India" or "Oslo, Norway". Use the most specific
                         version the signals support. Empty string "" if completely unknown.
  "location_city"      : specific city in English, or "" if no city signal
  "location_region"    : state/county/region in English (e.g. "England", "Maharashtra"), or ""
  "location_country"   : ISO 3166-1 alpha-2 UPPERCASE: "GB", "NO", "IN", "DK", "SE", "FI", "AU", or ""
  "location_confidence": float 0.0-1.0
                           1.0 = full address with postcode
                           0.8 = city name in content or phone area code match
                           0.6 = city in company name or keywords
                           0.3 = TLD only (country known, city unknown)
                           0.1 = pure guess
  "location_source"    : one of "address", "phone", "postcode", "content", "company_name", "domain", "inferred"

Rules:
  - location_country MUST be ISO 3166-1 alpha-2 (2 letters uppercase). "GB" not "UK".
  - location_full should read naturally: "City, Region, Country" — omit any part that is unknown.
  - Set location_city = "" if you have no specific city signal.
  - If clearly international/global with no single HQ, set all location fields to "".
  - Return ONLY the JSON array. No markdown fences, no explanation.
"""


def _user_prompt(batch: list[dict]) -> str:
    lines = []
    for s in batch:
        lines.append(json.dumps({
            "lead_id":  s.get("lead_id", s.get("domain", "")),
            "domain":   s.get("domain", ""),
            "summary":  (s.get("ai_summary") or "")[:300],
            "keywords": (s.get("keywords") or [])[:10],
            "country":  s.get("country", ""),
            "ai_country": s.get("ai_country", ""),
        }, ensure_ascii=False))
    return "Classify the location for each site:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Firestore / OpenAI init
# ---------------------------------------------------------------------------

def _load_secrets():
    """Return (fb_key_dict, openai_api_key) from blueboot_secrets.py."""
    secrets_path = Path(__file__).parent.parent / "blueboot_secrets.py"
    if not secrets_path.exists():
        return None, None
    try:
        spec = importlib.util.spec_from_file_location("blueboot_secrets", secrets_path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fb_key  = getattr(mod, "fireBaseAdminKey", None)
        cfg     = getattr(mod, "openAiConfig", {})
        api_key = cfg.get("defaultProjectKey") if isinstance(cfg, dict) else None
        return fb_key, api_key
    except Exception as e:
        print(f"  [location-enrich] secrets load error: {e}")
        return None, None


def _init_firestore(fb_key_dict):
    import firebase_admin
    from firebase_admin import firestore
    import firebase_admin.credentials as fb_creds
    cred = (fb_creds.Certificate(fb_key_dict) if fb_key_dict
            else fb_creds.Certificate(os.getenv("FIREBASE_CREDENTIALS", "config/serviceAccountKey.json")))
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    return firestore.client()


def _init_openai(api_key: str):
    from openai import AsyncOpenAI
    return AsyncOpenAI(api_key=api_key)


# ---------------------------------------------------------------------------
# Stream sites to enrich
# ---------------------------------------------------------------------------

def _stream_sites(db, countries: list[str] | None, limit: int | None, force: bool) -> list[tuple]:
    """Return list of (doc_ref, data_dict) for sites needing location enrichment."""
    col = db.collection(COLLECTION_DEFAULT)
    results = []

    print("  [location-enrich] Loading sites from Firestore…", flush=True)

    from google.cloud.firestore_v1.base_query import FieldFilter

    SELECT_FIELDS = ["lead_id", "domain", "country", "ai_country",
                     "ai_summary", "keywords", "location_enriched_at"]

    seen_ids: set[str] = set()

    def _collect(query_or_col):
        for doc in query_or_col.select(SELECT_FIELDS).stream():
            if doc.id in seen_ids:
                continue
            seen_ids.add(doc.id)
            data = doc.to_dict() or {}
            if not force and data.get("location_enriched_at"):
                continue
            results.append((doc.reference, {**data, "lead_id": doc.id}))

    if countries:
        for cc in countries:
            cc = cc.upper()
            # Primary: ai_country (set by site_enrich_agent)
            _collect(col.where(filter=FieldFilter("ai_country", "==", cc)))
            # Fallback: raw country field (for sites not yet AI-enriched)
            _collect(col.where(filter=FieldFilter("country", "==", cc)))
            if limit and len(results) >= limit:
                results[:] = results[:limit]
                return results
    else:
        _collect(col)
        if limit:
            results[:] = results[:limit]

    return results


# ---------------------------------------------------------------------------
# Async enrichment
# ---------------------------------------------------------------------------

async def _enrich_batch(
    client,
    loop,
    batch_refs:  list,
    batch_data:  list[dict],
    counters:    dict,
    dry_run:     bool,
) -> None:
    """Send one batch of up to 50 sites to OpenAI and write results back."""
    now_ts = datetime.now(timezone.utc).isoformat()

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=OPENAI_MODEL,
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user",   "content": _user_prompt(batch_data)},
                    ],
                    response_format={"type": "json_object"} if False else None,
                ),
                timeout=60.0,
            )
            raw = response.choices[0].message.content or "[]"
            # Strip markdown fences if present
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                # Model may wrap in {"locations": [...]}
                parsed = next((v for v in parsed.values() if isinstance(v, list)), [])
            if not isinstance(parsed, list):
                raise ValueError(f"unexpected response shape: {type(parsed)}")
            break
        except (json.JSONDecodeError, ValueError) as e:
            print(f"    [location-enrich] parse error attempt {attempt}: {e}")
            if attempt == RETRY_ATTEMPTS:
                counters["failed"] += len(batch_data)
                return
            await asyncio.sleep(RETRY_DELAY)
        except asyncio.TimeoutError:
            print(f"    [location-enrich] timeout attempt {attempt}")
            if attempt == RETRY_ATTEMPTS:
                counters["failed"] += len(batch_data)
                return
            await asyncio.sleep(RETRY_DELAY)

    # Build lookup by lead_id
    print(f"    [ai-result] {len(parsed)} records returned", flush=True)
    for r in parsed[:3]:   # show first 3 for brevity
        print(f"      {r.get('lead_id','?'):40s}  city={r.get('location_city','')!r}  "
              f"country={r.get('location_country','')!r}  conf={r.get('location_confidence',0):.2f}  "
              f"src={r.get('location_source','')!r}", flush=True)
    if len(parsed) > 3:
        print(f"      … and {len(parsed)-3} more", flush=True)

    result_map: dict[str, dict] = {r.get("lead_id", ""): r for r in parsed}

    for ref, data in zip(batch_refs, batch_data):
        lead_id = data.get("lead_id", "")
        res     = result_map.get(lead_id, {})

        # ISO alpha-2 → internal country code (GB→UK, rest stay as-is)
        _ISO_TO_CC: dict[str, str] = {
            "GB": "UK", "US": "US", "AU": "AU", "NZ": "NZ",
            "IN": "IN", "NO": "NO", "SE": "SE", "DK": "DK",
            "FI": "FI", "IE": "IE", "ZA": "ZA", "DE": "DE",
            "FR": "FR", "ES": "ES", "IT": "IT", "NL": "NL",
            "BE": "BE", "PL": "PL",
        }

        city          = (res.get("location_city")    or "").strip()
        region        = (res.get("location_region")  or "").strip()
        iso_country   = (res.get("location_country") or "").strip().upper()
        country_cc    = _ISO_TO_CC.get(iso_country, iso_country)  # GB→UK, rest unchanged
        location_full = (res.get("location_full")    or "").strip()
        confidence    = float(res.get("location_confidence", 0.0))
        source        = (res.get("location_source")  or "inferred").strip()

        # Fallback: build location_full from parts if model didn't return it
        if not location_full:
            parts = [p for p in [city, region] if p]
            if country_cc:
                parts.append(country_cc)
            location_full = ", ".join(parts)

        domain = data.get("domain", lead_id)
        flag   = f"  [{location_full or '?'}]  cc={country_cc}  conf={confidence:.2f}  src={source}"
        print(f"  {domain:45s}{flag}", flush=True)

        if dry_run:
            counters["dry_run"] += 1
            continue

        updates = {
            "location":             location_full,   # primary full-text field e.g. "London, England, UK"
            "location_full":        location_full,
            "location_city":        city,
            "location_region":      region,
            "location_country":     country_cc,      # internal code: UK, NO, IN, DK, SE, FI...
            "location_confidence":  confidence,
            "location_source":      source,
            "location_enriched_at": now_ts,
        }
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, lambda r=ref, u=updates: r.set(u, merge=True)),
                timeout=12.0,
            )
            counters["updated"] += 1
        except Exception as e:
            print(f"    [location-enrich] write error for {domain}: {e}")
            counters["failed"] += 1


async def _run_async(
    client,
    to_process: list[tuple],
    batch_size: int,
    concurrent: int,
    dry_run:    bool,
) -> dict:
    loop     = asyncio.get_running_loop()
    sem      = asyncio.Semaphore(concurrent)   # max 3 parallel OpenAI calls
    counters = {"total": len(to_process), "updated": 0, "failed": 0, "dry_run": 0}

    # Split into batches of batch_size
    batches = [to_process[i:i + batch_size] for i in range(0, len(to_process), batch_size)]
    total_b = len(batches)
    # Per-batch timeout: 60s OpenAI + 12s * batch_size writes
    batch_timeout = 90.0 + 12.0 * batch_size
    print(f"  [location-enrich] {len(to_process)} sites  ->  {total_b} batches of <={batch_size}"
          f"  (concurrent={concurrent}  batch_timeout={batch_timeout:.0f}s)", flush=True)

    async def _safe_batch(idx: int, batch: list[tuple]) -> None:
        async with sem:
            refs  = [r for r, _ in batch]
            datas = [d for _, d in batch]
            print("\n  [batch " + str(idx+1) + "/" + str(total_b) + "] " + str(len(batch)) + " sites  (slot acquired)", flush=True)
            try:
                await asyncio.wait_for(
                    _enrich_batch(client, loop, refs, datas, counters, dry_run),
                    timeout=batch_timeout,
                )
            except asyncio.TimeoutError:
                print(f"  [batch {idx+1}/{total_b}] TIMEOUT after {batch_timeout:.0f}s — skipping", flush=True)
                counters["failed"] += len(batch)
            except Exception as exc:
                print(f"  [batch {idx+1}/{total_b}] ERROR: {exc}", flush=True)
                counters["failed"] += len(batch)

    tasks = [asyncio.create_task(_safe_batch(i, b)) for i, b in enumerate(batches)]

    # Overall hard ceiling: all batches must finish within this time
    overall_timeout = batch_timeout * (total_b / concurrent + 2)
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=overall_timeout,
        )
        # Log any unhandled exceptions from gather
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                print(f"  [batch {i+1}] unhandled exception: {r}", flush=True)
    except asyncio.TimeoutError:
        print(f"  [location-enrich] OVERALL TIMEOUT ({overall_timeout:.0f}s) — cancelling remaining tasks", flush=True)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    print("\n[location-enrich] All batches complete.", flush=True)

    return counters


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def enrich_locations(
    countries:  list[str] | None = None,
    limit:      int | None       = None,
    batch_size: int              = BATCH_SIZE,
    concurrent: int              = CONCURRENT_BATCHES,
    dry_run:    bool             = False,
    force:      bool             = False,
) -> None:
    fb_key, api_key = _load_secrets()
    api_key = api_key or os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("No OpenAI API key found.")

    db     = _init_firestore(fb_key)
    client = _init_openai(api_key)

    to_proc = _stream_sites(db, countries, limit, force)
    if not to_proc:
        print("  [location-enrich] Nothing to enrich.")
        return

    print(f"\n  [location-enrich] Sites to process : {len(to_proc)}")
    print(f"  [location-enrich] Batch size       : {batch_size}")
    print(f"  [location-enrich] Concurrent       : {concurrent}")
    print(f"  [location-enrich] Model            : {OPENAI_MODEL}")
    print(f"  [location-enrich] Dry run          : {dry_run}")

    counters = asyncio.run(_run_async(client, to_proc, batch_size, concurrent, dry_run))

    print(f"\n  [location-enrich] Done.")
    print(f"    updated  : {counters['updated']}")
    print(f"    failed   : {counters['failed']}")
    if dry_run:
        print(f"    dry-run  : {counters['dry_run']} (no writes)")


def main() -> None:
    p = argparse.ArgumentParser(description="Enrich site_leads with AI-inferred company location")
    p.add_argument("--countries",   nargs="+", default=None, metavar="CC",
                   help="Filter by country codes e.g. UK IN NO")
    p.add_argument("--batch-size",  type=int, default=BATCH_SIZE, metavar="N",
                   help=f"Sites per OpenAI call (default {BATCH_SIZE})")
    p.add_argument("--concurrent",  type=int, default=CONCURRENT_BATCHES, metavar="N",
                   help="Parallel OpenAI batches (default " + str(CONCURRENT_BATCHES) + ")")
    p.add_argument("--limit",       type=int, default=None, metavar="N",
                   help="Max sites to process")
    p.add_argument("--dry-run",     type=int, default=None, metavar="N",
                   help="Dry-run on N sites: print results, skip Firestore writes")
    p.add_argument("--force",       action="store_true",
                   help="Re-enrich sites that already have location_enriched_at")
    p.add_argument("--collection",  default=COLLECTION_DEFAULT, metavar="NAME",
                   help="Firestore collection (default: " + COLLECTION_DEFAULT + ")")
    args = p.parse_args()

    dry_run = args.dry_run is not None
    limit   = args.dry_run if dry_run else args.limit

    countries = None
    if args.countries:
        raw = []
        for token in args.countries:
            raw.extend(c.strip().upper() for c in token.split(",") if c.strip())
        countries = raw or None

    enrich_locations(
        countries  = countries,
        limit      = limit,
        batch_size = args.batch_size,
        concurrent = args.concurrent,
        dry_run    = dry_run,
        force      = args.force,
    )


if __name__ == "__main__":
    main()
