"""site_email_check.py — Classify site_contacts by email type and contact role.

Reads site_contacts that have an email address (skipping those already checked),
sends batches of 50 to OpenAI to classify the email type and contact role, then
writes results back to each contact document.

Fields written to site_contacts:
  email_type        -- personal / role / department / admin
  contact_type      -- decision_maker / marketing / developer / sales / operations / unknown
  outreach_priority -- 1 (best) to 4 (lowest) — combined score
  email_checked_at  -- ISO timestamp

Outreach priority logic:
  1 = personal email + decision_maker or marketing role
  2 = personal email + other role  OR  role email + decision_maker
  3 = role / department email + non-admin contact type
  4 = admin email OR unknown contact type with generic email

Usage:
    python app\\site_email_check.py --countries UK --dry-run 20
    python app\\site_email_check.py --countries UK
    python app\\site_email_check.py --countries IN --force
    python app\\site_email_check.py --countries UK --batch-size 50 --concurrent 4
"""
from __future__ import annotations

import threading as _threading

# Guards firebase_admin.initialize_app against concurrent init
_local_fb_lock = _threading.Lock()
import argparse
import asyncio
import concurrent.futures as _futures
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import _pathsetup  # noqa: F401
from functions.config import cfg

BATCH_SIZE         = 50
CONCURRENT_BATCHES = 3
RETRY_ATTEMPTS     = 3
RETRY_DELAY        = 6.0

_SYSTEM_PROMPT = """\
You are a B2B email contact classifier. Given a list of contacts with their email, name,
title, and domain, classify each one for sales outreach targeting.

For each contact determine:

EMAIL TYPE — what kind of email address it is:
  personal    = clearly a named individual (firstname.lastname@, f.lastname@, first@domain)
  role        = generic role inbox (info@, hello@, contact@, sales@, support@, enquiries@)
  department  = department inbox (marketing@, hr@, accounts@, team@, press@, media@)
  admin       = technical/admin (webmaster@, admin@, postmaster@, noreply@, root@)

CONTACT TYPE — what role the person likely holds (use name + title + email signals):
  decision_maker = CEO, founder, director, MD, owner, managing director, partner, head of
  marketing      = marketing manager, digital marketing, content, SEO, social media, CMO
  developer      = web developer, CTO, engineer, tech lead, developer, programmer
  sales          = sales, business development, account manager, BDM, commercial
  operations     = operations, manager (generic/unclear), coordinator, admin
  unknown        = no clear signals available

OUTREACH PRIORITY — combined score 1-4:
  1 = personal email AND (decision_maker OR marketing) — direct line to decision maker
  2 = personal email AND other role  OR  role/dept email AND decision_maker
  3 = role or department email AND non-admin contact type
  4 = admin email OR unknown type with generic email

Return a JSON object with a single key "contacts" whose value is an array —
one object per input contact — each with exactly these keys:
  "contact_id"        : same string as in the input — never change it
  "email_type"        : one of personal / role / department / admin
  "contact_type"      : one of decision_maker / marketing / developer / sales / operations / unknown
  "outreach_priority" : integer 1, 2, 3, or 4
  "reasoning"         : one short sentence (max 15 words) explaining the classification

Return ONLY the JSON object, e.g. {"contacts": [ ... ]}. No markdown, no explanation.
"""


def _user_prompt(batch: list[dict]) -> str:
    items = []
    for c in batch:
        items.append(json.dumps({
            "contact_id": c.get("contact_id", ""),
            "email":      c.get("email", ""),
            "name":       (c.get("name") or "")[:60],
            "title":      (c.get("title") or c.get("occupation") or "")[:60],
            "domain":     c.get("domain", ""),
        }, ensure_ascii=False))
    return "Classify these contacts:\n" + "\n".join(items)


# ---------------------------------------------------------------------------
# Secrets / Firestore / OpenAI
# ---------------------------------------------------------------------------

def _load_secrets():
    """Load Firebase credentials from env (FIREBASE_KEY_JSON or FIREBASE_CREDENTIALS)."""
    from dotenv import load_dotenv
    load_dotenv()
    from functions.firebase_cred import get_firebase_cred
    return get_firebase_cred()


def _init_firestore(fb_key):
    import firebase_admin
    from firebase_admin import firestore
    import firebase_admin.credentials as creds
    cred = creds.Certificate(fb_key) if fb_key else creds.Certificate(
        cfg.FIREBASE_CREDENTIALS or "config/serviceAccountKey.json")
    with _local_fb_lock:
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
    return firestore.client()


def _init_openai(api_key):
    from openai import AsyncOpenAI
    return AsyncOpenAI(api_key=api_key)


# ---------------------------------------------------------------------------
# Load contacts
# ---------------------------------------------------------------------------

# Minimal field sets keep the matching pass lightweight - only marker fields
# travel over the wire until a contact is matched to a fully-enriched site.
_CONTACT_MATCH_FIELDS = ["email", "country", "ai_country",
                         "brave_enriched_at", "email_checked_at"]
_SITE_MATCH_FIELDS    = ["ai_classified_at", "location_enriched_at"]

_GET_CHUNK       = 300   # documents per get_all batch
_SCAN_PARTITIONS = 32    # parallel slices for the contact scan
_SCAN_WORKERS    = 8     # threads for the parallel scan
_READ_WORKERS    = 8     # threads for parallel get_all batches


def _scan_one_partition(query, countries, force) -> list:
    """Stream one collection-group partition (marker fields only) and return the
    references of contacts that pass the contact-level checks."""
    out = []
    for doc in query.stream():
        data = doc.to_dict() or {}
        if not (data.get("email") or "").strip():
            continue
        if not (data.get("brave_enriched_at") or "").strip():
            continue                                   # site_contact_enrich not done -> not ready
        if not force and (data.get("email_checked_at") or "").strip():
            continue                                   # already email-checked -> skip
        if countries:
            cc = (data.get("country") or data.get("ai_country") or "").upper()
            if cc not in countries:
                continue
        out.append(doc.reference)
    return out


def _parallel_get_all(db, refs: list, field_paths, workers: int, label: str) -> list:
    """Batched, parallel db.get_all over refs. Returns a list of snapshots."""
    chunks = [refs[i:i + _GET_CHUNK] for i in range(0, len(refs), _GET_CHUNK)]
    snaps, done, total = [], 0, len(refs)
    with _futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(lambda c=c: list(db.get_all(c, field_paths=field_paths))): c
                for c in chunks}
        for fut in _futures.as_completed(futs):
            snaps.extend(fut.result())
            done += len(futs[fut])
            print(f"  [enriched] {label} {done:>6,}/{total:,}", flush=True)
    return snaps


def _load_contacts(db, countries: list[str] | None, limit: int | None,
                   force: bool,
                   scan_partitions: int = _SCAN_PARTITIONS,
                   scan_workers: int = _SCAN_WORKERS,
                   read_workers: int = _READ_WORKERS) -> list[tuple]:
    """Load contacts READY for email-check but NOT yet email-checked.

    Ready = prior enrichment stages complete:
        contact doc : brave_enriched_at (site_contact_enrich)
        parent site : ai_classified_at  (site_enrich_agent)
                      location_enriched_at (site_location_enrich)
    AND the contact has NO email_checked_at yet (unless --force re-runs all).

    Phase 1 (minimal reads) - scan contacts in parallel partitions reading only
      marker fields, batch-read parent sites' markers, then match.
    Phase 2 (full reads)    - fetch complete docs for matched contacts only.
    """
    cg = db.collection_group("site_contacts")

    # ---- Phase 1a: parallel contact scan (marker fields only) ----
    print(f"  [enriched] Phase 1a - scanning contacts in up to {scan_partitions} "
          f"parallel partitions (markers only)…", flush=True)
    try:
        partitions = list(cg.get_partitions(scan_partitions))
        queries    = [p.query().select(_CONTACT_MATCH_FIELDS) for p in partitions]
    except Exception as exc:
        print(f"  [enriched] partitioning unavailable ({exc}); single-pass scan", flush=True)
        queries = [cg.select(_CONTACT_MATCH_FIELDS).order_by("__name__")]

    cand_refs: list = []
    done_parts = 0
    with _futures.ThreadPoolExecutor(max_workers=scan_workers) as ex:
        futs = [ex.submit(_scan_one_partition, q, countries, force) for q in queries]
        for fut in _futures.as_completed(futs):
            cand_refs.extend(fut.result())
            done_parts += 1
            print(f"  [enriched] …partition {done_parts}/{len(queries)} done  "
                  f"contact-matched so far {len(cand_refs):>6,}", flush=True)
    print(f"  [enriched] Phase 1a done - {len(cand_refs):,} contacts pass contact checks",
          flush=True)
    if not cand_refs:
        return []

    # ---- Phase 1b: parallel parent-site marker read (deduped) ----
    parent_by_path = {r.parent.parent.path: r.parent.parent for r in cand_refs}
    site_refs = list(parent_by_path.values())
    print(f"  [enriched] Phase 1b - checking {len(site_refs):,} unique parent sites…",
          flush=True)
    enriched_sites: set = set()
    for snap in _parallel_get_all(db, site_refs, _SITE_MATCH_FIELDS,
                                  read_workers, "…sites checked"):
        if not snap.exists:
            continue
        s = snap.to_dict() or {}
        if (s.get("ai_classified_at") or "").strip() and            (s.get("location_enriched_at") or "").strip():
            enriched_sites.add(snap.reference.path)
    print(f"  [enriched] Phase 1b done - {len(enriched_sites):,} sites fully enriched",
          flush=True)

    # ---- Phase 1c: match contacts to fully-enriched sites ----
    matched_refs = [r for r in cand_refs if r.parent.parent.path in enriched_sites]
    if limit:
        matched_refs = matched_refs[:limit]
    print(f"  [enriched] Phase 1c - {len(matched_refs):,} contacts matched to enriched sites",
          flush=True)
    if not matched_refs:
        return []

    # ---- Phase 2: parallel full-detail load for matched contacts ----
    print(f"  [enriched] Phase 2 - loading full detail for {len(matched_refs):,} contacts…",
          flush=True)
    results: list = []
    for snap in _parallel_get_all(db, matched_refs, None, read_workers, "…detail"):
        if not snap.exists:
            continue
        data = snap.to_dict() or {}
        contact_id = snap.reference.path.split("/")[-1]
        results.append((snap.reference, {**data, "contact_id": contact_id}))
    print(f"  [enriched] Done - {len(results):,} fully-enriched contacts loaded", flush=True)
    return results


# ---------------------------------------------------------------------------
# Async enrichment
# ---------------------------------------------------------------------------

def _compute_priority(email_type: str, contact_type: str) -> int:
    if email_type == "personal":
        if contact_type in ("decision_maker", "marketing"):
            return 1
        return 2
    if email_type in ("role", "department"):
        if contact_type == "decision_maker":
            return 2
        if contact_type != "unknown":
            return 3
    return 4


async def _enrich_batch(client, loop, batch_refs, batch_data, counters, dry_run):
    now_ts = datetime.now(timezone.utc).isoformat()

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=cfg.OPENAI_MODEL,
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user",   "content": _user_prompt(batch_data)},
                    ],
                    response_format={"type": "json_object"},
                ),
                timeout=60.0,
            )
            raw = response.choices[0].message.content or "[]"
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                parsed = next((v for v in parsed.values() if isinstance(v, list)), [])
            if not isinstance(parsed, list):
                raise ValueError(f"unexpected shape: {type(parsed)}")
            break
        except (json.JSONDecodeError, ValueError) as e:
            if attempt == RETRY_ATTEMPTS:
                counters["failed"] += len(batch_data)
                return
            await asyncio.sleep(RETRY_DELAY)
        except asyncio.TimeoutError:
            if attempt == RETRY_ATTEMPTS:
                counters["failed"] += len(batch_data)
                return
            await asyncio.sleep(RETRY_DELAY)

    result_map = {r.get("contact_id", ""): r for r in parsed}

    for ref, data in zip(batch_refs, batch_data):
        cid = data.get("contact_id", "")
        res = result_map.get(cid, {})

        email_type   = (res.get("email_type")   or "unknown").strip()
        contact_type = (res.get("contact_type") or "unknown").strip()
        reasoning    = (res.get("reasoning")    or "").strip()[:100]
        priority     = _compute_priority(email_type, contact_type)

        domain = data.get("domain", cid)
        print(f"  {domain:35s}  {email_type:12s}  {contact_type:16s}  P{priority}  {reasoning[:40]}", flush=True)

        if dry_run:
            counters["dry_run"] += 1
            continue

        updates = {
            "email_type":        email_type,
            "contact_type":      contact_type,
            "outreach_priority": priority,
            "email_check_reasoning": reasoning,
            "email_checked_at":  now_ts,
        }
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, lambda r=ref, u=updates: r.set(u, merge=True)),
                timeout=12.0,
            )
            counters["updated"] += 1
        except Exception as e:
            print(f"    [email-check] write error for {domain}: {e}")
            counters["failed"] += 1


async def _run_async(client, to_process, batch_size, concurrent, dry_run):
    loop     = asyncio.get_running_loop()
    sem      = asyncio.Semaphore(concurrent)
    counters = {"total": len(to_process), "updated": 0, "failed": 0, "dry_run": 0}

    batches = [to_process[i:i+batch_size] for i in range(0, len(to_process), batch_size)]
    total_b = len(batches)
    batch_timeout = 90.0 + 12.0 * batch_size
    print(f"  [email-check] {len(to_process)} contacts  ->  {total_b} batches of <={batch_size}"
          f"  concurrent={concurrent}", flush=True)

    async def _safe(idx, batch):
        async with sem:
            refs  = [r for r, _ in batch]
            datas = [d for _, d in batch]
            print(f"\n  [batch {idx+1}/{total_b}] {len(batch)} contacts", flush=True)
            try:
                await asyncio.wait_for(
                    _enrich_batch(client, loop, refs, datas, counters, dry_run),
                    timeout=batch_timeout,
                )
            except asyncio.TimeoutError:
                print(f"  [batch {idx+1}/{total_b}] TIMEOUT", flush=True)
                counters["failed"] += len(batch)
            except Exception as exc:
                print(f"  [batch {idx+1}/{total_b}] ERROR: {exc}", flush=True)
                counters["failed"] += len(batch)

    tasks = [asyncio.create_task(_safe(i, b)) for i, b in enumerate(batches)]
    overall_timeout = batch_timeout * (total_b / concurrent + 2)
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=overall_timeout,
        )
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                print(f"  [batch {i+1}] unhandled: {r}", flush=True)
    except asyncio.TimeoutError:
        print(f"  [email-check] OVERALL TIMEOUT — cancelling", flush=True)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    print(f"\n  [email-check] Done.  updated={counters['updated']}  "
          f"failed={counters['failed']}  dry_run={counters['dry_run']}", flush=True)
    return counters


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description="Classify site_contacts by email type and contact role")
    p.add_argument("--countries",   nargs="+", default=None, metavar="CC",
                   help="Space or comma-separated country codes e.g. --countries NO SE UK")
    p.add_argument("--batch-size",  type=int, default=BATCH_SIZE, metavar="N",
                   help=f"Contacts per OpenAI call (default {BATCH_SIZE})")
    p.add_argument("--concurrent",  type=int, default=CONCURRENT_BATCHES, metavar="N",
                   help=f"Parallel OpenAI batches (default {CONCURRENT_BATCHES})")
    p.add_argument("--limit",       type=int, default=None, metavar="N",
                   help="Max contacts to process")
    p.add_argument("--dry-run",     type=int, default=None, metavar="N",
                   help="Dry-run on N contacts: print results, skip Firestore writes")
    p.add_argument("--force",       action="store_true",
                   help="Re-classify contacts that already have email_checked_at")
    p.add_argument("--scan-partitions", type=int, default=_SCAN_PARTITIONS, metavar="N",
                   help=f"Parallel Firestore scan partitions for loading (default {_SCAN_PARTITIONS})")
    p.add_argument("--scan-workers", type=int, default=_SCAN_WORKERS, metavar="N",
                   help=f"Threads for the parallel contact scan (default {_SCAN_WORKERS})")
    p.add_argument("--read-workers", type=int, default=_READ_WORKERS, metavar="N",
                   help=f"Threads for parallel batched get_all reads (default {_READ_WORKERS})")
    args = p.parse_args(argv)

    dry_run = args.dry_run is not None
    limit   = args.dry_run if dry_run else args.limit

    countries = None
    if args.countries:
        raw = []
        for t in args.countries:
            raw.extend(c.strip().upper() for c in t.split(",") if c.strip())
        countries = raw or None

    fb_key, api_key = _load_secrets()
    api_key = api_key or cfg.OPENAI_API_KEY
    if not api_key:
        raise RuntimeError("No OpenAI API key found.")

    db     = _init_firestore(fb_key)
    client = _init_openai(api_key)

    to_proc = _load_contacts(
        db, countries, limit, args.force,
        scan_partitions=args.scan_partitions,
        scan_workers=args.scan_workers,
        read_workers=args.read_workers,
    )
    if not to_proc:
        print("  [email-check] Nothing to classify.")
        return

    print(f"\n  [email-check] Contacts : {len(to_proc)}")
    print(f"  [email-check] Model    : {OPENAI_MODEL}")
    print(f"  [email-check] Dry run  : {dry_run}")

    asyncio.run(_run_async(client, to_proc, args.batch_size, args.concurrent, dry_run))


if __name__ == "__main__":
    main()
