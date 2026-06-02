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
import argparse
import asyncio
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import _pathsetup  # noqa: F401

OPENAI_MODEL       = "gpt-5.4-mini"
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

Return a JSON array — one object per contact — with exactly these keys:
  "contact_id"        : same string as in the input — never change it
  "email_type"        : one of personal / role / department / admin
  "contact_type"      : one of decision_maker / marketing / developer / sales / operations / unknown
  "outreach_priority" : integer 1, 2, 3, or 4
  "reasoning"         : one short sentence (max 15 words) explaining the classification

Return ONLY the JSON array. No markdown, no explanation.
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
    p = Path(__file__).parent.parent / "blueboot_secrets.py"
    if not p.exists():
        return None, None
    try:
        spec = importlib.util.spec_from_file_location("blueboot_secrets", p)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fb_key  = getattr(mod, "fireBaseAdminKey", None)
        cfg     = getattr(mod, "openAiConfig", {})
        api_key = cfg.get("defaultProjectKey") if isinstance(cfg, dict) else None
        return fb_key, api_key
    except Exception as e:
        print(f"  [email-check] secrets error: {e}")
        return None, None


def _init_firestore(fb_key):
    import firebase_admin
    from firebase_admin import firestore
    import firebase_admin.credentials as creds
    c = creds.Certificate(fb_key) if fb_key else creds.Certificate(
        os.getenv("FIREBASE_CREDENTIALS", "config/serviceAccountKey.json"))
    with _local_fb_lock:
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

def _load_contacts(db, countries: list[str] | None, limit: int | None,
                   force: bool) -> list[tuple]:
    """Return list of (doc_ref, data_dict) for contacts needing classification."""
    from google.cloud.firestore_v1.base_query import FieldFilter

    cg = db.collection_group("site_contacts")
    results = []
    PAGE_SIZE = 500
    last_doc  = None

    print("  [email-check] Loading contacts from Firestore…", flush=True)

    while True:
        q = cg.order_by("__name__").limit(PAGE_SIZE)
        if last_doc:
            q = q.start_after(last_doc)
        page = list(q.stream())
        if not page:
            break
        last_doc = page[-1]

        for doc in page:
            data = doc.to_dict() or {}
            if not (data.get("email") or "").strip():
                continue
            if not force and data.get("email_checked_at"):
                continue

            # Country filter via parent lead
            if countries:
                c = (data.get("country") or data.get("ai_country") or "").upper()
                if c not in countries:
                    continue

            # Build contact_id from doc path
            parts = doc.reference.path.split("/")
            contact_id = parts[-1] if parts else doc.id

            results.append((doc.reference, {**data, "contact_id": contact_id}))
            if limit and len(results) >= limit:
                return results

        if len(page) < PAGE_SIZE:
            break

    print(f"  [email-check] {len(results)} contacts to classify", flush=True)
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
                    model=OPENAI_MODEL,
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user",   "content": _user_prompt(batch_data)},
                    ],
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
                   help="Country codes e.g. UK IN NO")
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
    api_key = api_key or os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("No OpenAI API key found.")

    db     = _init_firestore(fb_key)
    client = _init_openai(api_key)

    to_proc = _load_contacts(db, countries, limit, args.force)
    if not to_proc:
        print("  [email-check] Nothing to classify.")
        return

    print(f"\n  [email-check] Contacts : {len(to_proc)}")
    print(f"  [email-check] Model    : {OPENAI_MODEL}")
    print(f"  [email-check] Dry run  : {dry_run}")

    asyncio.run(_run_async(client, to_proc, args.batch_size, args.concurrent, dry_run))


if __name__ == "__main__":
    main()
