"""enrich_contacts.py — enrich Firestore contact docs with social media profiles.

For each contact in leads/{lead_id}/contacts/{id} that has a valid email + name:
  - Searches Bing for personal LinkedIn, Twitter/X, Facebook, Instagram, Telegram
  - Derives WhatsApp link from the contact's phone number (no search needed)
  - Updates the contact doc with found profile URLs

New fields written to each contact doc:
  linkedin_personal, twitter, facebook, instagram, telegram, whatsapp,
  social_enriched_at

Usage:
    python app/enrich_contacts.py [options]

Options:
    --collection NAME   Firestore leads collection      (default: leads)
    --country CODE      Filter by country code(s), comma-separated, e.g. NO,SE
    --limit N           Max contacts to process
    --workers N         Parallel async workers          (default: 20)
    --delay FLOAT       Seconds between Bing searches per worker (default: 1.0)
    --skip-enriched     Skip contacts that already have social_enriched_at set
    --platforms LIST    Comma-separated subset to search:
                        linkedin,twitter,facebook,instagram,telegram,whatsapp
                        Default: all
    --dry-run           Print what would be written without touching Firestore
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

import _pathsetup  # noqa: F401

# ---------------------------------------------------------------------------
# Browser user-agent
# ---------------------------------------------------------------------------

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Async Bing search
# ---------------------------------------------------------------------------

def _require_all(raw: str) -> str:
    """Prefix every bare word with + so Bing requires it (mirrors search_runner)."""
    return " ".join(
        w if w.startswith(("+", "-", '"')) else "+" + w
        for w in raw.split()
    )


async def _bing_search_async(
    session: aiohttp.ClientSession,
    query: str,
    max_results: int = 5,
) -> list[str]:
    """Async Bing RSS search. Returns up to max_results result URLs."""
    q = _require_all(query)
    try:
        async with session.get(
            "https://www.bing.com/search",
            params={"q": q, "format": "rss", "count": max_results, "first": 1},
            headers={
                "User-Agent":      _BROWSER_UA,
                "Accept":          "application/rss+xml,*/*",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            text = await resp.text()
        root = ET.fromstring(text)
        urls = []
        for item in root.findall(".//item"):
            link = item.find("link")
            if link is not None and (link.text or "").startswith("http"):
                urls.append(link.text.strip())
        return urls[:max_results]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Platform search specs
# ---------------------------------------------------------------------------

PLATFORMS: dict[str, dict] = {
    "linkedin": {
        "query":   '"{name}" "{company}" site:linkedin.com/in/',
        "pattern": re.compile(r'linkedin\.com/in/[^/"?&\s]+', re.IGNORECASE),
        "field":   "linkedin_personal",
        "prefix":  "https://www.",
    },
    "twitter": {
        "query":   '"{name}" "{company}" site:twitter.com OR site:x.com',
        "pattern": re.compile(
            r'(?:twitter|x)\.com/(?!search|home|i/|intent)[^/"?&\s]+',
            re.IGNORECASE,
        ),
        "field":   "twitter",
        "prefix":  "https://",
    },
    "facebook": {
        "query":   '"{name}" "{company}" site:facebook.com',
        "pattern": re.compile(
            r'facebook\.com/(?!sharer|share|login|pages/category)[^/"?&\s]+',
            re.IGNORECASE,
        ),
        "field":   "facebook",
        "prefix":  "https://www.",
    },
    "instagram": {
        "query":   '"{name}" "{company}" site:instagram.com',
        "pattern": re.compile(r'instagram\.com/[^/"?&\s]+', re.IGNORECASE),
        "field":   "instagram",
        "prefix":  "https://www.",
    },
    "telegram": {
        "query":   '"{name}" "{company}" site:t.me',
        "pattern": re.compile(r't\.me/[^/"?&\s]+', re.IGNORECASE),
        "field":   "telegram",
        "prefix":  "https://",
    },
}

# ---------------------------------------------------------------------------
# WhatsApp helper — derived from phone, no search needed
# ---------------------------------------------------------------------------

_COUNTRY_DIALCODES: dict[str, str] = {
    "NO": "47", "SE": "46", "DK": "45", "DE": "49",
    "UK": "44", "GB": "44", "FR": "33", "ES": "34",
    "NL": "31", "FI": "358", "PL": "48", "IT": "39",
    "PT": "351", "BE": "32", "AT": "43", "CH": "41",
    "US": "1",  "CA": "1",  "AU": "61",
}


def _whatsapp_link(phone: str, country: str = "") -> str:
    if not phone:
        return ""
    cleaned = re.sub(r"[^\d+]", "", phone.strip())
    if not cleaned:
        return ""
    if cleaned.startswith("+"):
        digits = cleaned[1:]
    elif cleaned.startswith("00"):
        digits = cleaned[2:]
    else:
        dialcode = _COUNTRY_DIALCODES.get((country or "").upper(), "")
        digits   = dialcode + cleaned if dialcode else cleaned
    if not re.fullmatch(r"\d{7,15}", digits):
        return ""
    return f"https://wa.me/{digits}"


# ---------------------------------------------------------------------------
# Firebase helpers
# ---------------------------------------------------------------------------

def _get_db(collection: str | None = None):
    try:
        import firebase_admin
        from firebase_admin import firestore
        import firebase_admin.credentials as fb_creds
    except ImportError:
        print("[enrich] firebase-admin not installed — run: pip install firebase-admin")
        return None, None

    secrets_path = Path(__file__).parent.parent / "blueboot_secrets.py"
    key_dict = None
    if secrets_path.exists():
        try:
            spec = importlib.util.spec_from_file_location("blueboot_secrets", secrets_path)
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            key_dict = getattr(mod, "fireBaseAdminKey", None)
        except Exception as e:
            print(f"[enrich] could not load blueboot_secrets: {e}")

    cred = (fb_creds.Certificate(key_dict) if key_dict
            else fb_creds.Certificate(os.getenv("FIREBASE_CREDENTIALS",
                                                "config/serviceAccountKey.json")))
    col_name = collection or os.getenv("FIRESTORE_COLLECTION", "leads")
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    db  = firestore.client()
    col = db.collection(col_name)
    return db, col


# ---------------------------------------------------------------------------
# Async worker — enrich one contact
# ---------------------------------------------------------------------------

async def _enrich_one(
    session:       aiohttp.ClientSession,
    semaphore:     asyncio.Semaphore,
    ref,                        # Firestore DocumentReference
    c:             dict,        # contact data
    platform_keys: list[str],
    do_whatsapp:   bool,
    delay:         float,
    counters:      dict,        # shared mutable counters (single-threaded asyncio)
    pending:       list,        # shared list of (ref, updates) to write
    dry_run:       bool,
) -> None:
    async with semaphore:
        name    = (c.get("name") or "").strip()
        company = (c.get("company") or "").strip()
        raw_ph  = (c.get("phone") or "").strip()
        phone   = raw_ph.split(",")[0].strip() if raw_ph else ""
        country = (c.get("country") or "").strip().upper()

        updates: dict = {}

        # WhatsApp — no network call needed
        if do_whatsapp and phone and not c.get("whatsapp"):
            wa = _whatsapp_link(phone, country)
            if wa:
                updates["whatsapp"] = wa

        # Bing searches — one per platform
        if name and company:
            for key in platform_keys:
                spec  = PLATFORMS[key]
                field = spec["field"]
                if c.get(field):
                    continue                                   # already populated
                query = spec["query"].format(name=name, company=company)
                urls  = await _bing_search_async(session, query, max_results=5)
                await asyncio.sleep(delay)
                for url in urls:
                    m = spec["pattern"].search(url)
                    if m:
                        updates[field] = spec["prefix"] + m.group(0)
                        break

        n = counters["processed"] + 1
        counters["processed"] = n

        if updates:
            updates["social_enriched_at"] = (
                datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
            )
            counters["updated"] += 1
            label = f"{name or c.get('email', '')} / {company}"
            found = [k for k in updates if k != "social_enriched_at"]
            print(f"  [{n}] {label}")
            for f in found:
                print(f"        {f:<20} {updates[f]}")
            pending.append((ref, updates))
        else:
            if n % 50 == 0:
                label = f"{name or c.get('email', '')} / {company}"
                print(f"  [{n}] {label} — nothing found")


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

_VALID_EMAIL_RE = re.compile(
    r'^[a-zA-Z0-9_.+\-]+@[a-zA-Z0-9\-]+(?:\.[a-zA-Z0-9\-]+)+$'
)
_PLACEHOLDER_EMAIL_RE = re.compile(
    r'(example|test|noemail|noreply|no-reply|donotreply|invalid|'
    r'localhost|placeholder|dummy|sample|fake|info@example|'
    r'user@example|admin@example|[0-9a-f]{16,}@)',
    re.IGNORECASE,
)

def _is_valid_email(email: str) -> bool:
    """Return True if email looks like a real, reachable address."""
    if not email or "@" not in email:
        return False
    local, _, domain = email.partition("@")
    if not local or not domain or "." not in domain:
        return False
    if not _VALID_EMAIL_RE.match(email):
        return False
    if _PLACEHOLDER_EMAIL_RE.search(email):
        return False
    # Reject suspiciously long hex local parts (automated/hash addresses)
    if len(local) >= 16 and re.fullmatch(r'[0-9a-f\-]+', local):
        return False
    return True


def enrich_contacts(
    collection:    str | None    = None,
    countries:     list[str] | None = None,
    limit:         int | None    = None,
    workers:       int           = 50,
    delay:         float         = 1.0,
    skip_enriched: bool          = False,
    platforms:     list[str] | None = None,
    dry_run:       bool          = False,
) -> None:
    """Enrich contact docs in Firestore with social media profile URLs."""

    active_platforms = platforms or list(PLATFORMS.keys()) + ["whatsapp"]
    platform_keys    = [p for p in active_platforms if p in PLATFORMS]
    do_whatsapp      = "whatsapp" in active_platforms

    db, col = _get_db(collection)
    if col is None:
        raise RuntimeError("Could not connect to Firestore — check credentials.")

    col_name = collection or os.getenv("FIRESTORE_COLLECTION", "leads")

    # Build normalised country filter — accept both ISO code ("NO") and full
    # name ("Norway") since contacts store country as the ISO code after the
    # fix_contact_country migration, but may still have full names in older docs.
    country_filter: set[str] | None = None
    if countries:
        try:
            from functions.utils import load_country_configs
            configs = load_country_configs()
        except Exception:
            configs = {}
        country_filter = set()
        for c_raw in countries:
            c_up = c_raw.strip().upper()
            country_filter.add(c_up)
            for cfg_code, cfg in configs.items():
                if cfg_code.upper() == c_up and isinstance(cfg, dict) and cfg.get("name"):
                    country_filter.add(cfg["name"].upper())

    print(f"[enrich] Collection : {col_name}")
    print(f"[enrich] Platforms  : {', '.join(active_platforms)}")
    print(f"[enrich] Workers    : {workers} parallel  delay={delay}s")
    if country_filter:
        print(f"[enrich] Countries  : {country_filter}")
    if dry_run:
        print("[enrich] DRY RUN — no Firestore writes.\n")

    # ------------------------------------------------------------------
    # Step 1 — stream + filter contacts synchronously
    # ------------------------------------------------------------------
    print("[enrich] Scanning contacts…")

    to_process: list[tuple] = []     # (ref, c_dict)
    scanned = no_email = country_filtered = already_enriched = no_name = 0

    for cdoc in db.collection_group("contacts").stream():
        scanned += 1
        c = cdoc.to_dict() or {}

        email = (c.get("email") or "").strip()
        if not _is_valid_email(email):
            no_email += 1
            continue

        if country_filter:
            raw = (c.get("country") or "").strip().upper()
            if raw not in country_filter:
                country_filtered += 1
                continue

        if skip_enriched and c.get("social_enriched_at"):
            already_enriched += 1
            continue

        name  = (c.get("name") or "").strip()
        phone = (c.get("phone") or "").strip()
        if not name and not phone:
            no_name += 1
            continue

        to_process.append((cdoc.reference, c))
        if limit and len(to_process) >= limit:
            break

    total = len(to_process)
    print(f"[enrich] {scanned} scanned → {total} to enrich  "
          f"(skipped: no email {no_email}, country {country_filtered}, "
          f"enriched {already_enriched}, no name/phone {no_name})")

    if total == 0:
        print("[enrich] Nothing to do.")
        return

    # ------------------------------------------------------------------
    # Step 2 — async parallel enrichment
    # ------------------------------------------------------------------
    counters: dict = {"processed": 0, "updated": 0}
    pending:  list = []    # (ref, updates) collected from workers

    async def _run_all():
        semaphore = asyncio.Semaphore(workers)
        connector = aiohttp.TCPConnector(limit=workers, ssl=False)
        timeout   = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            tasks = [
                _enrich_one(
                    session, semaphore, ref, c,
                    platform_keys, do_whatsapp, delay,
                    counters, pending, dry_run,
                )
                for ref, c in to_process
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(_run_all())

    # ------------------------------------------------------------------
    # Step 3 — batch write results to Firestore
    # ------------------------------------------------------------------
    if pending and not dry_run:
        MAX_BATCH      = 400
        PROGRESS_EVERY = 100
        written = 0
        batch   = db.batch()
        ops     = 0
        for ref, updates in pending:
            batch.update(ref, updates)
            ops     += 1
            written += 1
            if written % PROGRESS_EVERY == 0:
                print(f"  [enrich] {written}/{len(pending)} contacts written…")
            if ops >= MAX_BATCH:
                batch.commit()
                batch = db.batch()
                ops   = 0
        if ops:
            batch.commit()
        print(f"  [enrich] {written} contacts written to Firestore.")

    print(f"\n[enrich] Done.")
    print(f"  Scanned        : {scanned}")
    print(f"  Processed      : {counters['processed']}")
    print(f"  Updated        : {counters['updated']}")
    print(f"  Skipped        : {no_email + country_filtered + already_enriched + no_name}")
    print(f"    no valid email  : {no_email}")
    print(f"    country filter  : {country_filtered}")
    print(f"    already enriched: {already_enriched}")
    print(f"    no name/phone   : {no_name}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv=None):
    import argparse
    p = argparse.ArgumentParser(
        description="Enrich Firestore contact docs with social media profile URLs."
    )
    p.add_argument("--collection",    metavar="NAME", default=None,
                   help="Firestore leads collection (default: leads)")
    p.add_argument("--country",       metavar="CODE", action="append", dest="countries",
                   help="Country code(s) to filter on (repeatable or comma-separated)")
    p.add_argument("--countries",     metavar="CODES", default=None, dest="countries_alias",
                   help="Comma-separated country codes, e.g. --countries NO,SE,QQ")
    p.add_argument("--limit",         metavar="N", type=int, default=None,
                   help="Maximum number of contacts to process")
    p.add_argument("--workers",       metavar="N", type=int, default=50,
                   help="Number of parallel async workers (default: 50)")
    p.add_argument("--delay",         metavar="SECS", type=float, default=1.0,
                   help="Seconds to wait between Bing searches per worker (default: 1.0)")
    p.add_argument("--skip-enriched", action="store_true",
                   help="Skip contacts that already have social_enriched_at set")
    p.add_argument("--platforms",     metavar="LIST", default=None,
                   help="Comma-separated platforms: linkedin,twitter,facebook,"
                        "instagram,telegram,whatsapp  (default: all)")
    p.add_argument("--dry-run",       action="store_true",
                   help="Print what would be written without touching Firestore")
    return p.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)

    countries = None
    raw_countries = list(args.countries or [])
    # Also accept --countries NO,SE,QQ (alias)
    if getattr(args, "countries_alias", None):
        raw_countries.extend(args.countries_alias.split(","))
    if raw_countries:
        expanded = []
        for c in raw_countries:
            expanded.extend(x.strip().upper() for x in c.split(",") if x.strip())
        countries = expanded or None

    platforms = None
    if args.platforms:
        platforms = [p.strip().lower() for p in args.platforms.split(",") if p.strip()]

    if countries:
        print(f"[enrich] Country filter: {countries}")
    if platforms:
        print(f"[enrich] Platform filter: {platforms}")

    enrich_contacts(
        collection=args.collection,
        countries=countries,
        limit=args.limit,
        workers=args.workers,
        delay=args.delay,
        skip_enriched=args.skip_enriched,
        platforms=platforms,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
