"""lead_enrich_agent.py -- AI classification of leads collection via GPT.

Reads unclassified documents from the `leads` Firestore collection, sends them
in batches to GPT for agency-specific classification, then writes results back.

Fields written to each leads doc:
  ai_sector          -- e.g. "web_agency", "seo_agency", "marketing_agency"
  ai_specialisation  -- array of service tags e.g. ["wordpress", "woocommerce", "seo"]
  ai_client_base     -- "SMB" | "enterprise" | "mixed" | "local" | "unknown"
  ai_reseller_potential -- "high" | "medium" | "low"
  ai_platform        -- CMS/site builder they use themselves
  ai_summary         -- one-sentence description
  ai_confidence      -- float 0.0-1.0
  ai_classified_at   -- ISO timestamp

Usage:
    python app/lead_enrich_agent.py
    python app/lead_enrich_agent.py --countries NO,SE
    python app/lead_enrich_agent.py --countries NO --limit 200
    python app/lead_enrich_agent.py --force          # re-classify already classified
    python app/lead_enrich_agent.py --dry-run
    python app/lead_enrich_agent.py --batch-size 10 --concurrent 5
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

COLLECTION_DEFAULT  = "leads"
OPENAI_MODEL        = "gpt-5.4-mini"
BATCH_SIZE          = 10
CONCURRENT_BATCHES  = 3
RETRY_ATTEMPTS      = 3
RETRY_DELAY         = 5.0

SECTORS = [
    "web_agency", "seo_agency", "design_agency", "marketing_agency",
    "hosting_provider", "ecommerce_agency", "communication_agency",
    "it_consulting", "pr_agency", "media_agency", "other",
]

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a B2B agency classifier. You receive a list of web agencies and digital "
    "service providers and must classify each one for sales targeting purposes.\n\n"
    "These are pre-screened companies — they are likely web agencies, WordPress/WooCommerce "
    "providers, digital agencies, SEO firms, design studios, or communication agencies.\n\n"
    "For each lead analyse:\n"
    "  - What specific digital services they offer (web development, SEO, design, "
    "hosting, WooCommerce, e-commerce, etc.)\n"
    "  - How strong a reseller fit they are for a B2B SaaS search/discovery product\n"
    "  - What CMS or platform they specialise in building with\n"
    "  - What CMS or platform they use on their own site\n"
    "  - Their likely client base (SMB local businesses, enterprises, international, etc.)\n"
    "  - What country they operate from\n\n"
    "For each lead return an object with exactly these keys:\n"
    '  "lead_id"              : same string as in the input — never change it\n'
    '  "sector"               : one of ' + json.dumps(SECTORS) + "\n"
    '  "specialisation"       : array of up to 6 lowercase tags from: '
    '["wordpress", "woocommerce", "shopify", "webflow", "squarespace", "custom_dev", '
    '"seo", "sem", "web_design", "branding", "hosting", "ecommerce", "social_media", '
    '"email_marketing", "analytics", "ux", "app_dev", "drupal", "magento"]\n'
    '  "client_base"          : one of ["SMB", "enterprise", "mixed", "local", "unknown"]\n'
    '  "reseller_potential"   : one of ["high", "medium", "low"] — '
    "how likely this agency is to resell or recommend a B2B SaaS product to their clients. "
    "High = active WordPress/WooCommerce shop with SMB clients; "
    "Low = large enterprise-only firm or unrelated sector\n"
    '  "platform"             : CMS/site builder the agency uses on their own website '
    '(e.g. "WordPress", "Webflow", "custom", "unknown")\n'
    '  "summary"              : one sentence (max 20 words) describing what the agency does\n'
    '  "confidence"           : float 0.0-1.0 reflecting how certain you are\n\n'
    "Return ONLY a valid JSON array — no markdown, no explanation, no extra keys."
)


def _user_prompt(batch: list[dict]) -> str:
    items = [
        {
            "lead_id":       lead["lead_id"],
            "url":           lead.get("website", ""),
            "title":         (lead.get("title") or "")[:120],
            "description":   (lead.get("description") or "")[:200],
            "categories":    lead.get("categories", []),
            "detected_tech": lead.get("detected_tech", []),
            "reasons":       (lead.get("reasons") or "")[:200],
            "country":       lead.get("country", ""),
            "reseller_score": lead.get("reseller_score", 0),
        }
        for lead in batch
    ]
    return json.dumps(items, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Secrets / Firestore / OpenAI
# ---------------------------------------------------------------------------

def _load_secrets():
    secrets_path = Path(__file__).parent.parent / "blueboot_secrets.py"
    if not secrets_path.exists():
        return None, None
    try:
        spec = importlib.util.spec_from_file_location("blueboot_secrets", secrets_path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fb_key  = getattr(mod, "fireBaseAdminKey", None)
        ai_cfg  = getattr(mod, "openAiConfig", {})
        api_key = ai_cfg.get("defaultProjectKey") or ai_cfg.get("apiKey") or ""
        return fb_key, api_key
    except Exception as e:
        print(f"  [lead-enrich] could not load blueboot_secrets: {e}")
        return None, None


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
    return firestore.client()


def _init_openai(api_key: str):
    try:
        from openai import AsyncOpenAI
    except ImportError:
        raise RuntimeError("openai not installed — run: pip install openai")
    return AsyncOpenAI(api_key=api_key)


# ---------------------------------------------------------------------------
# Firestore scan
# ---------------------------------------------------------------------------

def _stream_unclassified(
    db,
    collection: str,
    countries:  list[str] | None,
    force:      bool,
    limit:      int | None,
) -> dict:
    """Return {lead_id: (doc_ref, doc_dict)} for leads to classify."""
    print(f"  [lead-enrich] Scanning '{collection}'…")
    col     = db.collection(collection)
    ref_map = {}
    scanned = skipped_done = skipped_country = 0

    for doc in col.stream():
        scanned += 1
        data = doc.to_dict() or {}

        if countries:
            c = (data.get("country") or "").upper()
            if c not in countries:
                skipped_country += 1
                continue

        if not force and data.get("ai_classified_at"):
            skipped_done += 1
            continue

        data["lead_id"] = doc.id
        ref_map[doc.id] = (doc.reference, data)

        if limit and len(ref_map) >= limit:
            break

    print(
        f"  [lead-enrich] {scanned} scanned → {len(ref_map)} to classify  "
        f"(skipped: {skipped_done} already done, {skipped_country} wrong country)"
    )
    return ref_map


# ---------------------------------------------------------------------------
# Async GPT batch call
# ---------------------------------------------------------------------------

async def _classify_batch(
    client:    object,
    semaphore: asyncio.Semaphore,
    batch_data: list[dict],
    batch_num:  int,
    batch_tot:  int,
) -> list[dict]:
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        sleep_secs = 0.0
        result     = None

        async with semaphore:
            print(f"  [lead-enrich] batch {batch_num}/{batch_tot}  ({len(batch_data)} leads) → GPT…")
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
                if isinstance(parsed, dict):
                    parsed = next((v for v in parsed.values() if isinstance(v, list)), [])
                if isinstance(parsed, list):
                    print(f"  [lead-enrich] batch {batch_num}/{batch_tot}  ✓ {len(parsed)} results")
                    return parsed
                print(f"  [lead-enrich] batch {batch_num} unexpected shape: {type(parsed)}")
                return []
            except json.JSONDecodeError as e:
                print(f"  [lead-enrich] batch {batch_num} JSON error (attempt {attempt}): {e}")
                sleep_secs = RETRY_DELAY
            except Exception as e:
                err = str(e)
                print(f"  [lead-enrich] batch {batch_num} GPT error (attempt {attempt}): {err}")
                if "rate_limit" in err.lower() or "429" in err:
                    sleep_secs = RETRY_DELAY * attempt
                else:
                    sleep_secs = RETRY_DELAY

        if sleep_secs and attempt < RETRY_ATTEMPTS:
            await asyncio.sleep(sleep_secs)

    print(f"  [lead-enrich] batch {batch_num} failed after {RETRY_ATTEMPTS} attempts")
    return []


# ---------------------------------------------------------------------------
# Write results
# ---------------------------------------------------------------------------

async def _process_batch(
    client:    object,
    semaphore: asyncio.Semaphore,
    loop:      asyncio.AbstractEventLoop,
    batch_ids: list[str],
    ref_map:   dict,
    batch_num: int,
    batch_tot: int,
    now_ts:    str,
    counters:  dict,
    dry_run:   bool,
) -> None:
    batch_data = [ref_map[lid][1] | {"lead_id": lid} for lid in batch_ids]
    results    = await _classify_batch(client, semaphore, batch_data, batch_num, batch_tot)

    result_map = {str(r.get("lead_id", "")): r for r in results if isinstance(r, dict)}

    for lid in batch_ids:
        r    = result_map.get(lid)
        orig = ref_map[lid][1]

        if not r:
            counters["done"]   += 1
            counters["failed"] += 1
            print(f"    [skip] no result for {orig.get('domain', lid)}")
            continue

        updates = {
            "ai_sector":             r.get("sector", "other"),
            "ai_specialisation":     r.get("specialisation", []),
            "ai_client_base":        r.get("client_base", "unknown"),
            "ai_reseller_potential": r.get("reseller_potential", "low"),
            "ai_platform":           r.get("platform", "unknown"),
            "ai_summary":            r.get("summary", ""),
            "ai_confidence":         float(r.get("confidence", 0.0)),
            "ai_classified_at":      now_ts,
        }

        total   = counters["total"]
        done    = counters["done"] + 1
        domain  = orig.get("domain", lid)
        sector  = updates["ai_sector"]
        pot     = updates["ai_reseller_potential"]
        conf    = updates["ai_confidence"]
        spec    = ", ".join(updates["ai_specialisation"][:4])
        smry    = (updates["ai_summary"] or "")[:65]
        print(f"    [{done}/{total}] {domain:<42} {sector}  {pot}  conf={conf:.2f}")
        if spec:
            print(f"           {spec}")
        if smry:
            print(f"           {smry}")

        counters["done"] += 1

        if not dry_run:
            doc_ref = ref_map[lid][0]
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, lambda r=doc_ref, u=updates: r.set(u, merge=True)),
                    timeout=12.0,
                )
                counters["classified"] += 1
            except Exception as exc:
                print(f"    [firestore] write error {domain}: {exc}")
                counters["failed"] += 1
        else:
            counters["classified"] += 1


# ---------------------------------------------------------------------------
# Async runner
# ---------------------------------------------------------------------------

async def _run_async(
    db,
    client,
    ref_map:    dict,
    batch_size: int,
    concurrent: int,
    dry_run:    bool,
) -> dict:
    loop      = asyncio.get_running_loop()
    semaphore = asyncio.Semaphore(concurrent)
    now_ts    = datetime.now(timezone.utc).isoformat()
    lead_ids  = list(ref_map.keys())
    batches   = [lead_ids[i:i+batch_size] for i in range(0, len(lead_ids), batch_size)]
    counters  = {
        "total":      len(lead_ids),
        "done":       0,
        "classified": 0,
        "failed":     0,
    }

    tasks = [
        asyncio.create_task(_process_batch(
            client, semaphore, loop,
            batch, ref_map, i+1, len(batches),
            now_ts, counters, dry_run,
        ))
        for i, batch in enumerate(batches)
    ]
    await asyncio.gather(*tasks, return_exceptions=True)
    return counters


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def enrich_leads(
    collection: str              = COLLECTION_DEFAULT,
    countries:  list[str] | None = None,
    limit:      int | None       = None,
    batch_size: int              = BATCH_SIZE,
    concurrent: int              = CONCURRENT_BATCHES,
    dry_run:    bool             = False,
    force:      bool             = False,
    api_key:    str | None       = None,
) -> None:
    fb_key, secret_key = _load_secrets()
    api_key = api_key or secret_key or os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "No OpenAI API key found. Set OPENAI_API_KEY or add defaultProjectKey "
            "to blueboot_secrets.py openAiConfig."
        )

    db     = _init_firestore(fb_key)
    client = _init_openai(api_key)

    ref_map = _stream_unclassified(db, collection, countries, force, limit)
    if not ref_map:
        print("  [lead-enrich] Nothing to classify.")
        return

    lead_ids  = list(ref_map.keys())
    batches   = [lead_ids[i:i+batch_size] for i in range(0, len(lead_ids), batch_size)]

    print(f"\n  [lead-enrich] Collection  : {collection}")
    print(f"  [lead-enrich] Model       : {OPENAI_MODEL}")
    print(f"  [lead-enrich] Leads       : {len(ref_map)}")
    print(f"  [lead-enrich] Batches     : {len(batches)} × {batch_size}")
    print(f"  [lead-enrich] Concurrent  : {concurrent} parallel GPT calls")
    print(f"  [lead-enrich] Dry run     : {dry_run}\n")

    started  = datetime.now(timezone.utc)
    counters = asyncio.run(_run_async(db, client, ref_map, batch_size, concurrent, dry_run))
    elapsed  = (datetime.now(timezone.utc) - started).total_seconds()

    print(f"\n  [lead-enrich] Done in {elapsed:.0f}s")
    print(f"  Total leads      : {counters['total']}")
    print(f"  Classified       : {counters['classified']}")
    print(f"  Failed           : {counters['failed']}")
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
        description="AI classification of leads collection via GPT"
    )
    p.add_argument("--collection",  default=COLLECTION_DEFAULT, metavar="NAME",
                   help=f"Firestore collection name  (default: {COLLECTION_DEFAULT})")
    p.add_argument("--countries",   default=None, metavar="CODES",
                   help="Comma-separated country codes  e.g. NO,SE  (default: all)")
    p.add_argument("--limit",       type=int, default=None, metavar="N",
                   help="Max leads to classify")
    p.add_argument("--batch-size",  type=int, default=BATCH_SIZE, metavar="N",
                   help=f"Leads per GPT call  (default: {BATCH_SIZE})")
    p.add_argument("--concurrent",  type=int, default=CONCURRENT_BATCHES, metavar="N",
                   help=f"Parallel GPT calls  (default: {CONCURRENT_BATCHES})")
    p.add_argument("--dry-run",     action="store_true",
                   help="Print results without writing to Firestore")
    p.add_argument("--force",       action="store_true",
                   help="Re-classify leads that already have ai_classified_at")

    args = p.parse_args(argv)

    countries = None
    if args.countries:
        countries = [c.strip().upper() for c in args.countries.split(",") if c.strip()]

    enrich_leads(
        collection = args.collection,
        countries  = countries,
        limit      = args.limit,
        batch_size = args.batch_size,
        concurrent = args.concurrent,
        dry_run    = args.dry_run,
        force      = args.force,
    )


if __name__ == "__main__":
    main()
