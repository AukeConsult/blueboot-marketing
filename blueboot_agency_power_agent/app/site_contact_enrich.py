"""site_contact_enrich.py — Enrich site_contacts sub-collection via Brave Search + GPT.

Reads every document from the site_contacts collectionGroup
(path: site_leads/{lead_id}/site_contacts/{contact_id}), runs a Brave Search
per contact (name + company), then uses GPT to extract / confirm:
  occupation, company, linkedin, twitter, facebook, other_links

Results are written back to the same contact document.

Existing fields on each contact doc (written by site_agent):
  email, name, title, phone, domain, website, country, found_on

New fields written by this script:
  occupation    -- confirmed / enriched job title
  company       -- confirmed company name
  linkedin      -- LinkedIn profile URL
  twitter       -- Twitter/X profile URL
  facebook      -- Facebook profile URL
  other_links   -- list of other relevant URLs
  brave_enriched_at -- ISO timestamp

Usage:
    python app/site_contact_enrich.py
    python app/site_contact_enrich.py --countries NO,SE
    python app/site_contact_enrich.py --limit 100 --dry-run
    python app/site_contact_enrich.py --force        # re-enrich already enriched contacts
    python app/site_contact_enrich.py --concurrent 5
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import _pathsetup  # noqa: F401

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LEADS_COLLECTION    = "site_leads"
CONTACTS_COLLECTION = "site_contacts"
OPENAI_MODEL        = "gpt-4.1-mini"
CONCURRENT_DEFAULT  = 15     # parallel Brave+GPT tasks
CONTACT_TIMEOUT     = 45.0   # hard ceiling per contact
RETRY_ATTEMPTS      = 2
SEARCH_RESULTS      = 5      # Brave results to fetch per contact query

# ---------------------------------------------------------------------------
# Secrets / clients
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
        print(f"  [contact-enrich] could not load blueboot_secrets: {e}")
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
# Firestore scan — collectionGroup
# ---------------------------------------------------------------------------

def _stream_contacts(
    db,
    countries: list[str] | None,
    limit:     int | None,
    force:     bool,
) -> list[tuple]:
    """Stream site_contacts collectionGroup → [(doc_ref, doc_dict), ...]."""
    print(f"  [contact-enrich] Scanning collectionGroup '{CONTACTS_COLLECTION}'…")
    col     = db.collection_group(CONTACTS_COLLECTION)
    results = []
    scanned = skipped = 0

    for doc in col.stream():
        scanned += 1
        data = doc.to_dict() or {}

        # Country filter
        if countries:
            c = (data.get("country") or "").upper()
            if c not in countries:
                skipped += 1
                continue

        # Must have a name to search for
        if not (data.get("name") or "").strip():
            skipped += 1
            continue

        # Skip already enriched unless --force
        if not force and data.get("brave_enriched_at"):
            skipped += 1
            continue

        results.append((doc.reference, data))
        if limit and len(results) >= limit:
            break

    print(
        f"  [contact-enrich] {scanned} scanned → {len(results)} to enrich  "
        f"({skipped} skipped)"
    )
    return results


# ---------------------------------------------------------------------------
# Brave search (sync — runs in executor thread)
# ---------------------------------------------------------------------------

def _brave_search(query: str, country_code: str = "") -> list[dict]:
    """Search Brave and return result dicts with url/title/description."""
    import requests as _req

    api_key = os.getenv("BRAVE_API_KEY", "")
    if not api_key:
        print(f"      [brave] SKIP — BRAVE_API_KEY not set")
        return []

    # Map internal country codes to ISO 3166-1 alpha-2 for Brave API
    _CC_MAP = {
        "uk": "gb", "UK": "gb",
        "en": "gb", "EN": "gb",
        "qq": "",   "QQ": "",   # global — no country filter
    }
    cc = country_code.lower() if country_code else ""
    cc = _CC_MAP.get(country_code, _CC_MAP.get(cc, cc))
    params = {
        "q":          query,
        "count":      SEARCH_RESULTS,
        "safesearch": "off",
    }
    if cc:
        params["country"] = cc

    print(f"      [brave] searching: {query!r}  country={cc or 'any'}")
    t0 = time.monotonic()
    try:
        resp = _req.get(
            "https://api.search.brave.com/res/v1/web/search",
            params=params,
            headers={
                "Accept":               "application/json",
                "Accept-Encoding":      "gzip",
                "X-Subscription-Token": api_key,
            },
            timeout=15,
        )
        elapsed = time.monotonic() - t0
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        elapsed = time.monotonic() - t0
        print(f"      [brave] ERROR after {elapsed:.1f}s: {e}")
        return []

    results = []
    for item in data.get("web", {}).get("results", []):
        results.append({
            "url":         item.get("url", ""),
            "title":       item.get("title", ""),
            "description": item.get("description", ""),
        })

    urls_preview = "  ".join(r["url"][:55] for r in results[:2]) if results else "no results"
    print(f"      [brave] {len(results)} results in {elapsed:.1f}s  {urls_preview}")
    return results


# ---------------------------------------------------------------------------
# GPT extraction
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a contact information extractor. Given a person's name, their company/domain, "
    "and a list of Brave web search results, extract professional information about this person.\n\n"
    "Return a JSON object with exactly these keys:\n"
    '  "occupation"  : confirmed job title or role, or ""\n'
    '  "company"     : confirmed company or organisation name, or ""\n'
    '  "linkedin"    : full LinkedIn profile URL, or ""\n'
    '  "twitter"     : Twitter/X profile URL, or ""\n'
    '  "facebook"    : Facebook profile URL, or ""\n'
    '  "other_links" : array of other relevant URLs (personal site, company profile, etc.)\n\n'
    "Only include information clearly about this specific person. "
    "Return ONLY a valid JSON object — no markdown, no explanation."
)


def _user_prompt(name: str, domain: str, title: str, results: list[dict]) -> str:
    return json.dumps({
        "name":           name,
        "domain":         domain,
        "known_title":    title,
        "search_results": results,
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Async pipeline
# ---------------------------------------------------------------------------

async def _enrich_one(
    client:   object,
    loop:     asyncio.AbstractEventLoop,
    doc_ref,
    data:     dict,
    counters: dict,
    dry_run:  bool,
) -> None:
    name    = (data.get("name") or "").strip()
    domain  = data.get("domain", "")
    title   = data.get("title", "")
    country = (data.get("country") or "").upper()
    if country in ("*", "GLOBAL", "ALL", "?"):
        country = ""   # wildcard — do not pass to Brave API
    query   = f"{name} {domain}".strip()

    counters["done"] += 1
    total = counters["total"]
    print(f"  [{counters['done']}/{total}] {name:<30} {domain}")

    # Brave search
    try:
        results = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: _brave_search(query, country)),
            timeout=20.0,
        )
    except Exception as exc:
        print(f"      [brave] failed: {exc}")
        results = []

    if not results:
        counters["no_results"] += 1
        return

    # GPT extraction
    extracted = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            t0 = time.monotonic()
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=OPENAI_MODEL,
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user",   "content": _user_prompt(name, domain, title, results)},
                    ],
                ),
                timeout=20.0,
            )
            gpt_elapsed = time.monotonic() - t0
            raw = response.choices[0].message.content or "{}"
            extracted = json.loads(raw)
            if not isinstance(extracted, dict):
                extracted = None
                raise ValueError(f"unexpected shape: {type(extracted)}")
            occ      = extracted.get("occupation", "")
            linkedin = extracted.get("linkedin", "")
            print(f"      [gpt]  {gpt_elapsed:.1f}s  occ={occ!r}  linkedin={linkedin!r}")
            break
        except json.JSONDecodeError as e:
            print(f"      [gpt]  JSON error (attempt {attempt}): {e}")
        except Exception as e:
            print(f"      [gpt]  error (attempt {attempt}): {e}")
            if attempt < RETRY_ATTEMPTS:
                await asyncio.sleep(2.0)

    if not extracted:
        counters["failed"] += 1
        return

    updates = {
        "occupation":        extracted.get("occupation", ""),
        "company":           extracted.get("company", ""),
        "linkedin":          extracted.get("linkedin", ""),
        "twitter":           extracted.get("twitter", ""),
        "facebook":          extracted.get("facebook", ""),
        "other_links":       extracted.get("other_links", []),
        "brave_enriched_at": datetime.now(timezone.utc).isoformat(),
    }

    if not dry_run:
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, lambda: doc_ref.set(updates, merge=True)),
                timeout=12.0,
            )
            counters["updated"] += 1
        except Exception as exc:
            print(f"      [firestore] write error: {exc}")
            counters["failed"] += 1
    else:
        counters["updated"] += 1


async def _run_async(
    db,
    client,
    to_process: list[tuple],
    concurrent: int,
    dry_run:    bool,
) -> dict:
    loop     = asyncio.get_running_loop()
    sem      = asyncio.Semaphore(concurrent)
    counters = {
        "total":      len(to_process),
        "done":       0,
        "updated":    0,
        "no_results": 0,
        "failed":     0,
    }

    async def _safe(ref, data):
        # Wait for semaphore BEFORE starting the timeout so queue wait
        # time does not count against the per-contact ceiling.
        async with sem:
            try:
                await asyncio.wait_for(
                    _enrich_one(client, loop, ref, data, counters, dry_run),
                    timeout=CONTACT_TIMEOUT,
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                counters["done"]   += 1
                counters["failed"] += 1
                print(f"      [timeout] {data.get('name', '?')}")
            except Exception as exc:
                counters["done"]   += 1
                counters["failed"] += 1
                print(f"      [error] {data.get('name', '?')}: {exc}")

    tasks = [asyncio.create_task(_safe(ref, data)) for ref, data in to_process]
    await asyncio.gather(*tasks, return_exceptions=True)
    return counters


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def enrich_contacts(
    countries:  list[str] | None = None,
    limit:      int | None       = None,
    concurrent: int              = CONCURRENT_DEFAULT,
    dry_run:    bool             = False,
    force:      bool             = False,
) -> None:
    fb_key, api_key = _load_secrets()
    api_key = api_key or os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("No OpenAI API key found.")
    if not os.getenv("BRAVE_API_KEY"):
        print("  [contact-enrich] WARNING: BRAVE_API_KEY not set — Brave search will be skipped.")

    db     = _init_firestore(fb_key)
    client = _init_openai(api_key)

    to_proc = _stream_contacts(db, countries, limit, force)
    if not to_proc:
        print("  [contact-enrich] Nothing to enrich.")
        return

    print(f"\n  [contact-enrich] Contacts to process : {len(to_proc)}")
    print(f"  [contact-enrich] Model               : {OPENAI_MODEL}")
    print(f"  [contact-enrich] Concurrent          : {concurrent}")
    print(f"  [contact-enrich] Dry run             : {dry_run}\n")

    started  = datetime.now(timezone.utc)
    counters = asyncio.run(_run_async(db, client, to_proc, concurrent, dry_run))
    elapsed  = (datetime.now(timezone.utc) - started).total_seconds()

    print(f"\n  [contact-enrich] Done in {elapsed:.0f}s")
    print(f"  Total processed : {counters['done']}")
    print(f"  Enriched        : {counters['updated']}")
    print(f"  No Brave results: {counters['no_results']}")
    print(f"  Failed          : {counters['failed']}")
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
        description="Enrich site_contacts collectionGroup via Brave Search + GPT"
    )
    p.add_argument("--countries",  default=None, metavar="CODES",
                   help="Comma-separated country codes  e.g. NO,SE  (default: all)")
    p.add_argument("--limit",      type=int, default=None, metavar="N",
                   help="Max contacts to process")
    p.add_argument("--concurrent", type=int, default=CONCURRENT_DEFAULT, metavar="N",
                   help=f"Parallel tasks  (default: {CONCURRENT_DEFAULT})")
    p.add_argument("--dry-run",    action="store_true",
                   help="Print results without writing to Firestore")
    p.add_argument("--force",      action="store_true",
                   help="Re-enrich contacts that already have brave_enriched_at set")

    args = p.parse_args(argv)

    countries = None
    if args.countries:
        countries = [c.strip().upper() for c in args.countries.split(",") if c.strip()]

    enrich_contacts(
        countries  = countries,
        limit      = args.limit,
        concurrent = args.concurrent,
        dry_run    = args.dry_run,
        force      = args.force,
    )


if __name__ == "__main__":
    main()
