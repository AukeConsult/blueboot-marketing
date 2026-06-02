"""Entry point — argument parsing, mode dispatch, and Firebase upload."""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import load_dotenv

# Ensure both the app/ directory and the project root are on sys.path so this
# script works whether invoked as `python app/lead_agent.py` (root as cwd) or
# from inside the app/ directory.
import _pathsetup  # noqa: F401 — adds project root, app/, app/functions/, app/collect-functions/ to sys.path

from catalog_scrapers import catalog_run          # noqa: E402
from search_runner import run                     # noqa: E402

from app.functions.utils import clean_str

try:
    from app.functions.models import lead_id_from_url
except ModuleNotFoundError:
    from functions.models import lead_id_from_url  # noqa: E402

if TYPE_CHECKING:
    from app.functions.models import Lead


import threading as _threading
_firebase_init_lock = _threading.Lock()   # guards firebase_admin.initialize_app

# ---------------------------------------------------------------------------
# Firebase upload
# ---------------------------------------------------------------------------

def load_leads_from_firebase(collection: str | None = None) -> set[str]:
    """Return the set of domains already stored in Firestore.

    Used to pre-populate seen_domains before scraping so already-crawled
    agencies are not re-visited even if the local Excel file is empty or absent.

    Returns an empty set if firebase-admin is not installed, credentials are
    missing, or the collection is empty.
    """
    try:
        import firebase_admin
        import firebase_admin.credentials as fb_creds
        from firebase_admin import firestore
    except ImportError:
        print("  [firebase] firebase-admin not installed — run: pip install firebase-admin")
        return set()

    # --- credentials (same logic as push_to_firebase) ---
    cred = None
    secrets_path = Path(__file__).parent.parent / "blueboot_secrets.py"
    if secrets_path.exists():
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("blueboot_secrets", secrets_path)
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            key_dict = getattr(mod, "fireBaseAdminKey", None)
            if key_dict:
                cred = fb_creds.Certificate(key_dict)
        except Exception as exc:
            print(f"  [firebase] could not load blueboot_secrets: {exc}")

    if cred is None:
        creds_path = os.getenv("FIREBASE_CREDENTIALS", "config/serviceAccountKey.json")
        if Path(creds_path).exists():
            cred = fb_creds.Certificate(creds_path)

    if cred is None and not firebase_admin._apps:
        print("  [firebase] no credentials found — skipping preload.")
        return set()

    col_name = collection or os.getenv("FIRESTORE_COLLECTION", "leads")
    with _firebase_init_lock:
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)

    db  = firestore.client()
    col = db.collection(col_name)

    # Partition the collection for parallel fetching (significant speedup on large collections)
    from concurrent.futures import ThreadPoolExecutor as _TPE

    PARTITIONS = 16
    try:
        queries = [p.query() for p in col.get_partitions(PARTITIONS)]
    except Exception:
        queries = [col.order_by("__name__")]

    def _fetch_partition(q):
        result = set()
        for doc in q.select(["domain"]).stream():
            d = (doc.to_dict() or {}).get("domain", "")
            if d:
                result.add(d.strip().lower())
        return result

    domains: set[str] = set()
    with _TPE(max_workers=PARTITIONS) as pool:
        futures = list(pool.map(_fetch_partition, queries, timeout=30.0))
    for partial in futures:
        domains |= partial   # merge in main thread after all workers done

    print(f"  [firebase] preloaded {len(domains)} existing domains from '{col_name}' ({len(queries)} partitions)")
    return domains



def load_leads_excluded() -> set[str]:
    """Return the set of domains already in leads_excluded.

    Merges with preloaded_domains at startup so excluded sites
    are never re-crawled in future runs.
    """
    try:
        import firebase_admin
        import firebase_admin.credentials as fb_creds
        from firebase_admin import firestore
    except ImportError:
        return set()

    cred = None
    secrets_path = Path(__file__).parent.parent / "blueboot_secrets.py"
    if secrets_path.exists():
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("blueboot_secrets", secrets_path)
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            key_dict = getattr(mod, "fireBaseAdminKey", None)
            if key_dict:
                cred = fb_creds.Certificate(key_dict)
        except Exception:
            pass

    if cred is None and not firebase_admin._apps:
        return set()
    with _firebase_init_lock:
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)

    db  = firestore.client()
    col = db.collection("leads_excluded")

    from concurrent.futures import ThreadPoolExecutor as _TPE

    PARTITIONS = 8
    try:
        queries = [p.query() for p in col.get_partitions(PARTITIONS)]
    except Exception:
        queries = [col.order_by("__name__")]

    def _fetch_excl(q):
        result = set()
        for doc in q.select(["domain"]).stream():
            d = (doc.to_dict() or {}).get("domain", "")
            if d:
                result.add(d.strip().lower())
        return result

    excluded: set[str] = set()
    with _TPE(max_workers=PARTITIONS) as pool:
        futures = list(pool.map(_fetch_excl, queries, timeout=30.0))
    for partial in futures:
        excluded |= partial   # merge in main thread after all workers done

    print(f"  [firebase] {len(excluded)} excluded leads loaded ({len(queries)} partitions)")
    return excluded


def push_to_firebase(leads: list["Lead"], collection: str | None = None) -> None:
    """Upsert leads + contacts into Firestore."""
    try:
        import firebase_admin
        import firebase_admin.credentials as fb_creds
        from firebase_admin import firestore
    except ImportError:
        print("  [firebase] firebase-admin not installed — run: pip install firebase-admin")
        return

    cred = None
    secrets_path = Path(__file__).parent.parent / "blueboot_secrets.py"
    if secrets_path.exists():
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("blueboot_secrets", secrets_path)
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            key_dict = getattr(mod, "fireBaseAdminKey", None)
            if key_dict:
                cred = fb_creds.Certificate(key_dict)
        except Exception as exc:
            print(f"  [firebase] could not load blueboot_secrets: {exc}")

    if cred is None:
        creds_path = os.getenv("FIREBASE_CREDENTIALS", "config/serviceAccountKey.json")
        if Path(creds_path).exists():
            cred = fb_creds.Certificate(creds_path)

    col_name = collection or os.getenv("FIRESTORE_COLLECTION", "leads")
    if cred is None and not firebase_admin._apps:
        print("  [firebase] no credentials found — skipping upload.")
        return
    with _firebase_init_lock:
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)

    db  = firestore.client()
    col = db.collection(col_name)

    def _lead_id(website: str) -> str:
        return lead_id_from_url(website)

    def _contact_id(email: str) -> str:
        return hashlib.sha1(email.lower().encode()).hexdigest()[:10]

    def _parse_contacts(lead: "Lead") -> list[dict]:
        emails = [e.strip() for e in lead.emails.split(",") if e.strip()] if lead.emails else []
        titles = [t.strip() for t in lead.email_titles.split(",")] if lead.email_titles else []
        return [
            {
                "email":    email,
                "title":    clean_str(titles[i]) if i < len(titles) else "",
                "lead_id":  _lead_id(lead.website),
                "company":  lead.company,
                "domain":   lead.domain,
                "website":  lead.website,
                "country":  lead.country_name,
                "phones":   lead.phones,
                "linkedin": lead.linkedin,
            }
            for i, email in enumerate(emails)
        ]

    MAX_BATCH     = 400
    PROGRESS_EVERY = 100
    batch         = db.batch()
    ops           = 0
    lead_count    = 0
    contact_count = 0

    def _flush():
        nonlocal batch, ops
        if ops:
            batch.commit()
        batch = db.batch()
        ops = 0

    for lead in leads:
        if not lead.domain:
            continue
        lid      = _lead_id(lead.website)
        lead_doc = asdict(lead)
        lead_doc["lead_id"] = lid
        lead_doc.pop("emails",       None)
        lead_doc.pop("email_titles", None)
        lead_doc.pop("email_names",  None)

        batch.set(col.document(lid), lead_doc, merge=True)
        ops        += 1
        lead_count += 1

        if lead_count % PROGRESS_EVERY == 0:
            print(f"  [firebase] {lead_count} leads written so far…")

        for contact in _parse_contacts(lead):
            cid = _contact_id(contact["email"])
            batch.set(
                col.document(lid).collection("contacts").document(cid),
                contact,
                merge=True,
            )
            ops           += 1
            contact_count += 1

        if ops >= MAX_BATCH:
            _flush()

    _flush()
    print(f"  [firebase] uploaded {lead_count} leads + {contact_count} contacts -> {col_name}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="BlueBoot Lead Agent -- find & score web-design agencies",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--mode", choices=["search", "catalog", "both", "audit"], default="both",
        help="search = Bing/Google keyword search; catalog = scrape directory listings; both = run catalog first, then search (default); audit = scan existing Firestore leads for TLD mismatches",
    )
    parser.add_argument(
        "--countries", default=None,
        help="Comma-separated ISO codes, e.g. NO,SE,DK. Default: all configured.",
    )
    parser.add_argument(
        "--queries", default=None,
        help="Path to a queries file (overrides per-country query files).",
    )
    parser.add_argument(
        "--output",
        default=str(Path(__file__).parent.parent / "output"),
        help="Output directory for the Excel file (default: <project_root>/output).",
    )
    parser.add_argument(
        "--max-results", type=int, default=int(os.getenv("MAX_RESULTS", "200")),
        help="Max search results per query.",
    )
    parser.add_argument(
        "--min-score", type=int, default=int(os.getenv("MIN_SCORE", "50")),
        help="Minimum reseller score to store a lead (default: 50).",
    )
    parser.add_argument(
        "--max-pages", type=int, default=int(os.getenv("MAX_PAGES", "6")),
        help="Max pages to crawl per agency website.",
    )
    parser.add_argument(
        "--max-country", type=int, default=int(os.getenv("MAX_COUNTRY", "1000")) or None,
        help="Stop a country after this many leads (0 = unlimited).",
    )
    parser.add_argument(
        "--give-up-after", type=int, default=int(os.getenv("GIVE_UP_AFTER", "5")),
        help="Give up a country after this many consecutive empty queries.",
    )
    parser.add_argument(
        "--delay", type=float, default=float(os.getenv("CRAWL_DELAY", "1.0")),
        help="Seconds to wait between page fetches within one site.",
    )
    parser.add_argument(
        "--workers", type=int, default=int(os.getenv("CRAWL_WORKERS", "20")),
        help="Parallel site-crawl workers / batch size.",
    )
    parser.add_argument(
        "--max-catalog-pages", type=int, default=None,
        help="Limit pages per catalog source (for testing).",
    )
    parser.add_argument(
        "--no-output", action="store_true", default=False,
        help="Skip writing the Excel output file after the run.",
    )
    parser.add_argument(
        "--no-firebase", action="store_true", default=False,
        help="Skip uploading results to Firestore after the run.",
    )
    parser.add_argument(
        "--no-github", action="store_true", default=False,
        help="Skip the GitHub org pre-pass (useful if GITHUB_TOKEN is not set).",
    )
    parser.add_argument(
        "--firebase-preload", action="store_true", default=False,
        help="Read existing domains from Firestore before scraping to skip already-crawled agencies.",
    )
    parser.add_argument(
        "--firebase-collection", default=None,
        help="Override Firestore collection name (default: 'leads').",
    )
    parser.add_argument(
        "--audit-dry-run", action="store_true", default=False,
        help="With --mode audit: print mismatches but do NOT delete anything.",
    )
    parser.add_argument(
        "--force", action="store_true", default=False,
        help="Re-crawl all sites — skip loading leads and leads_excluded from Firestore. "
             "Useful to rescan QQ or other global groups without the history filter.",
    )
    return parser


# ---------------------------------------------------------------------------
# TLD audit
# ---------------------------------------------------------------------------

def audit_tlds(collection: str | None = None, dry_run: bool = False) -> None:
    """Scan every Firestore lead for TLD mismatches, print a report, then
    delete the offending leads (and their contacts sub-collection).

    Pass dry_run=True (or --audit-dry-run on the CLI) to report only,
    without deleting anything.
    """
    try:
        import firebase_admin
        import firebase_admin.credentials as fb_creds
        from firebase_admin import firestore as _fs
    except ImportError:
        print("  [audit] firebase-admin not installed.")
        return

    from pathlib import Path as _Path
    try:
        from app.functions.utils import load_country_configs, tld_accepted_for, country_for_domain
    except ModuleNotFoundError:
        from functions.utils import load_country_configs, tld_accepted_for, country_for_domain

    # ---- Firebase init ----
    cred = None
    secrets_path = _Path(__file__).parent.parent / "blueboot_secrets.py"
    if secrets_path.exists():
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("blueboot_secrets", secrets_path)
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            key_dict = getattr(mod, "fireBaseAdminKey", None)
            if key_dict:
                cred = fb_creds.Certificate(key_dict)
        except Exception as exc:
            print(f"  [audit] could not load blueboot_secrets: {exc}")

    if cred is None:
        creds_path = os.getenv("FIREBASE_CREDENTIALS", "config/serviceAccountKey.json")
        if _Path(creds_path).exists():
            cred = fb_creds.Certificate(creds_path)

    if cred is None:
        print("  [audit] no Firebase credentials found.")
        return

    col_name = collection or os.getenv("FIRESTORE_COLLECTION", "leads")
    with _firebase_init_lock:
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)

    db  = _fs.client()
    col = db.collection(col_name)

    configs   = load_country_configs()
    all_codes = [k for k in configs.keys() if k != "global_tlds"]  # skip the settings key

    try:
        from app.functions.utils import load_global_tlds
    except ModuleNotFoundError:
        from functions.utils import load_global_tlds

    global_tlds = load_global_tlds(configs)

    print(f"\n[audit] Scanning '{col_name}' for TLD corrections…")
    print(f"[audit] Global TLDs (→ country=*): {sorted(global_tlds)}")

    # (domain, current_country, correct_code, correct_name, doc_ref)
    # correct_code = "*" for global TLDs, real country code for ccTLD re-assignments
    corrections: list[tuple] = []

    # Leads whose TLD is unrecognised AND not global AND not in accepted_tlds → delete
    # (domain, tld_str, reason, doc_ref)
    deletions: list[tuple] = []

    total = 0
    checked = 0

    for doc in col.select(["domain", "country"]).stream():
        total += 1
        d       = doc.to_dict()
        domain  = (d.get("domain") or "").strip().lower()
        country = (d.get("country") or "").strip().upper()
        if not domain or not country or country == "*":
            continue
        checked += 1

        parts   = domain.split(".")
        tld_str = "." + ".".join(parts[-2:]) if len(parts) >= 3 else "." + parts[-1]

        # --- global TLD (.com / .org / .net / …) → tag as global ---
        if any(domain.endswith(t) for t in global_tlds):
            corrections.append((domain, country, "*", "global", doc.reference))
            continue

        detected = country_for_domain(domain, all_codes, configs)

        if detected and detected != country:
            # ccTLD unambiguously belongs to a different known country → re-assign
            correct_name = configs.get(detected, {}).get("name", detected)
            corrections.append((domain, country, detected, correct_name, doc.reference))

        elif detected is None and not tld_accepted_for(domain, country, configs):
            # Unknown TLD not accepted for this country → delete
            reason = f"TLD {tld_str!r} not accepted for {country}"
            deletions.append((domain, tld_str, reason, doc.reference))

    # ---- Print report ----
    print(f"[audit] Scanned {checked} leads (of {total} total).")

    # -- Corrections (re-assign country OR tag as global) --
    global_c  = [(d,w,c,n,r) for d,w,c,n,r in corrections if c == "*"]
    country_c = [(d,w,c,n,r) for d,w,c,n,r in corrections if c != "*"]

    if not corrections:
        print("[audit] No TLD corrections needed. ✓")
    else:
        tag = " [DRY RUN — no changes made]" if dry_run else ""

        if global_c:
            print(f"\n[audit] {len(global_c)} lead(s) with global TLD → country=* / global:{tag}\n")
            print(f"  {'Domain':<50}  Was")
            print(f"  {'-'*49}  {'-'*6}")
            for domain, was, _c, _n, _ref in sorted(global_c, key=lambda x: x[0]):
                print(f"  {domain:<50}  {was}")
            print()

        if country_c:
            print(f"\n[audit] {len(country_c)} lead(s) to re-assign to correct country:{tag}\n")
            print(f"  {'Domain':<50}  {'Was':<6}  →  Correct country")
            print(f"  {'-'*49}  {'-'*5}     {'-'*20}")
            for domain, was, code, name, _ref in sorted(country_c, key=lambda x: x[0]):
                print(f"  {domain:<50}  {was:<6}  →  {code}  ({name})")
            print()

        if not dry_run:
            updated = 0
            PROGRESS_EVERY = 100
            for _domain, was, correct_code, correct_name, ref in corrections:
                ref.update({
                    "country":          correct_code,
                    "country_name":     correct_name,
                    "country_original": was,
                })
                updated += 1
                if updated % PROGRESS_EVERY == 0:
                    print(f"[audit] {updated}/{len(corrections)} corrections applied…")
            print(f"[audit] Applied {updated} correction(s) "
                  f"({len(global_c)} global, {len(country_c)} country re-assign). ✓")

    # -- Deletions (truly unaccepted TLD) --
    if not deletions:
        print("[audit] No unaccepted-TLD leads to delete. ✓")
    else:
        tag = " [DRY RUN — nothing deleted]" if dry_run else ""
        print(f"\n[audit] {len(deletions)} lead(s) with unaccepted TLD:{tag}\n")
        for domain, tld, reason, _ref in sorted(deletions, key=lambda x: x[0]):
            print(f"  {domain:<50}  {reason}")
        print()

        if not dry_run:
            deleted_leads    = 0
            deleted_contacts = 0
            for _domain, _tld, _reason, ref in deletions:
                for contact_doc in ref.collection("contacts").stream():
                    contact_doc.reference.delete()
                    deleted_contacts += 1
                ref.delete()
                deleted_leads += 1
            print(f"[audit] Deleted {deleted_leads} lead(s) and {deleted_contacts} contact(s). ✓")

    # =========================================================================
    # PASS 2 — Contact audit: blank/invalid emails + foreign email domains
    #
    # Uses collection_group("contacts") to scan all contact documents in one
    # query.  Each contact document stores its lead's domain in the "domain"
    # field, so no parent lookup is needed.
    # Checks each contact for:
    #   • blank / whitespace-only email
    #   • malformed email (no @, or invalid characters / fake TLD)
    #   • obviously fake email domains (e.g. 20km@6.7l, price@tag.x)
    #   • email domain that doesn't match the lead's own domain
    #     (exception: .com / .org / .net variants of the same base name are OK)
    # =========================================================================
    import re as _re

    # A real email domain must:  letters/digits/hyphens, at least one dot,
    # and a TLD of 2–24 real letters (no digits, no single chars like "l").
    _VALID_EMAIL_RE = _re.compile(
        r'^[^@\s]+@[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)*\.[a-z]{2,24}$'
    )

    print("\n[audit] Scanning contacts for blank/invalid emails and foreign email domains…")

    bad_refs: list[tuple] = []   # (ref, email, lead_domain, reason)
    contact_total = 0

    for cdoc in db.collection_group("contacts").stream():
        contact_total += 1
        c     = cdoc.to_dict()
        email = (c.get("email") or "").strip().lower()
        lead_domain = (c.get("domain") or "").strip().lower()

        # ---- 1. blank / empty ----
        if not email:
            bad_refs.append((cdoc.reference, email, lead_domain, "blank email"))
            continue

        # ---- 2. regex validation (catches 20km@6.7l, price@tag, etc.) ----
        if not _VALID_EMAIL_RE.match(email):
            bad_refs.append((cdoc.reference, email, lead_domain,
                             f"invalid email format: {email!r}"))

    print(f"[audit] Scanned {contact_total} contact(s).")

    def _print_contact_block(label: str, refs: list) -> None:
        if not refs:
            print(f"  {label}: none found ✓")
            return
        print(f"  {label}: {len(refs)}")
        for _ref, email, domain, reason in sorted(refs, key=lambda x: (x[2], x[1])):
            print(f"    {domain:<40}  {email:<40}  {reason}")

    _print_contact_block("Blank / invalid emails", bad_refs)

    if dry_run:
        print(f"\n[audit] DRY RUN — would delete {len(bad_refs)} contact(s).")
    elif not bad_refs:
        print("[audit] No bad contacts to delete. ✓")
    else:
        deleted = 0
        for ref, _e, _d, _r in bad_refs:
            ref.delete()
            deleted += 1
        print(f"\n[audit] Deleted {deleted} bad contact(s). ✓")

    # =========================================================================
    # PASS 3 — Blocklist re-check on existing leads
    #   a) domain / website URL / company name matches a blocklist glob pattern
    #   b) title / description / company name contains a content-negative keyword
    # =========================================================================
    print("\n[audit] Re-checking existing leads against blocklist & content negative keywords…")

    try:
        from app.functions.utils import (
            is_blocked, load_lines, domain_of, _CONTENT_NEG_KWS as _NEG_KWS,
        )
    except ModuleNotFoundError:
        from functions.utils import (
            is_blocked, load_lines, domain_of, _CONTENT_NEG_KWS as _NEG_KWS,
            clean_str,
        )

    from pathlib import Path as _PPath
    _bl_path = _PPath(__file__).parent.parent / "config" / "blocklist_domains.txt"
    blocklist: set[str] = set(load_lines(_bl_path)) if _bl_path.exists() else set()

    # Build a plain-text word set from glob patterns for company-name matching.
    # Strip leading/trailing * so "*restaurant*" becomes "restaurant".
    _glob_words: list[str] = sorted(
        {p.strip("*").lower() for p in blocklist if "*" in p and len(p.strip("*")) >= 4},
        key=len, reverse=True  # longest first → fewer false positives
    )

    _ADULT_TERMS = {
        "porn", "pornhub", "pornography", "xvideos", "xhamster", "redtube",
        "youporn", "brazzers", "onlyfans", "chaturbate", "cam4", "livecam",
        "livejasmin", "webcam girls", "webcam sex", "camgirl", "camsite",
        "adult entertainment", "adult content", "adult film", "adult video",
        "erotic", "erotica", "erotik", "erotisch", "erotique", "erótico",
        "erotico", "erotyczny", "erotisk", "sexfilm", "sexvideo", "sex chat",
        "sexting", "escortservice", "escort service", "escort girl",
        "escort girls", "escorts", "incall", "outcall", "stripclub",
        "strip club", "lapdance", "peepshow", "striptease", "hentai",
        "anime porn", "milf", "fetish", "bdsm", "bondage", "dominatrix",
        "nudity", "nude", "naked", "naughty", "nsfw", "playboy",
        "penthouse", "hustler",
    }

    blocked_refs  = []   # (doc_ref, display_domain, reason)
    bl_total = 0

    for doc in col.select(["domain", "website", "company", "title", "description"]).stream():
        bl_total += 1
        d       = doc.to_dict()
        domain  = (d.get("domain")  or "").strip().lower()
        website = (d.get("website") or "").strip()
        company = (d.get("company") or "").strip()
        title   = (d.get("title")   or "").strip()
        desc    = (d.get("description") or "").strip()

        display = domain or website or company

        # ---- a1) domain field against blocklist globs ----
        if domain and is_blocked(domain, blocklist):
            blocked_refs.append((doc.reference, display, f"domain blocked: {domain}"))
            continue

        # ---- a2) website URL: extract domain and check again ----
        if website:
            try:
                url_domain = domain_of(website)
            except Exception:
                url_domain = ""
            if url_domain and url_domain != domain and is_blocked(url_domain, blocklist):
                blocked_refs.append((doc.reference, display,
                                     f"website domain blocked: {url_domain}"))
                continue

        # ---- a3) company name against blocklist glob word stems ----
        company_l = company.lower()
        glob_hit = next((w for w in _glob_words if w in company_l), None)
        if glob_hit:
            blocked_refs.append((doc.reference, display,
                                 f"blocklist word in company name: '{glob_hit}' in '{company}'"))
            continue

        # ---- b) content-negative keywords in title / description / company ----
        check_text = (title + " " + desc + " " + company).lower()
        if check_text.strip():
            adult_hits = [kw for kw in _ADULT_TERMS if kw in check_text]
            if adult_hits:
                blocked_refs.append((doc.reference, display,
                                     f"adult content in title/desc: {adult_hits[:4]!r}"))
                continue
            hits = [kw for kw in _NEG_KWS if kw in check_text]
            if hits:
                blocked_refs.append((doc.reference, display,
                                     f"neg keyword in title/company: {hits[:4]!r}"))


    print(f"[audit] Scanned {bl_total} lead(s).")

    if not blocked_refs:
        print("[audit] No blocklist or title-keyword violations found. ✓")
    else:
        print(f"  {'Domain':<45}  Reason")
        print(f"  {'-'*44}  {'-'*40}")
        for _ref, domain, reason in sorted(blocked_refs, key=lambda x: x[1]):
            print(f"  {domain:<45}  {reason}")

        if not dry_run:
            total_blocked = len(blocked_refs)
            print(f"\n[audit] Deleting {total_blocked} blocked lead(s)…")
            del_leads    = 0
            del_contacts = 0
            PROGRESS_EVERY = 100
            for ref, _domain, _reason in blocked_refs:
                for cdoc in ref.collection("contacts").stream():
                    cdoc.reference.delete()
                    del_contacts += 1
                ref.delete()
                del_leads += 1
                if del_leads % PROGRESS_EVERY == 0:
                    print(f"[audit] {del_leads}/{total_blocked} blocked leads deleted…")
            print(f"[audit] Deleted {del_leads} lead(s) and {del_contacts} contact(s). ✓")
        else:
            print(f"\n[audit] DRY RUN — would delete {len(blocked_refs)} blocked lead(s).")


def main() -> None:
    load_dotenv()
    args = _build_parser().parse_args()

    if getattr(args, "force", False):
        print("  [lead_agent] --force: skipping leads + leads_excluded preload — all sites will be re-crawled")
        args.preloaded_domains = set()
    else:
        # Run both preloads in parallel — each is partitioned internally
        from concurrent.futures import ThreadPoolExecutor as _TPE
        print("  [lead_agent] preloading leads + leads_excluded in parallel…", flush=True)
        with _TPE(max_workers=2) as _pool:
            _f_leads    = _pool.submit(load_leads_from_firebase, args.firebase_collection)
            _f_excluded = _pool.submit(load_leads_excluded)
            args.preloaded_domains = _f_leads.result()
            _excluded              = _f_excluded.result()
        args.preloaded_domains |= _excluded
        print(f"  [firebase] {len(args.preloaded_domains)} total domains in skip list "
              f"({len(_excluded)} excluded)", flush=True)

    if args.mode == "audit":
        audit_tlds(collection=args.firebase_collection, dry_run=args.audit_dry_run)
        return

    if args.mode == "catalog":
        leads = catalog_run(args)
    elif args.mode == "search":
        leads = run(args)
    else:  # "both"
        print("\n" + "="*60)
        print("PHASE 1 — Catalog scrape")
        print("="*60)
        leads = catalog_run(args) or []
        print("\n" + "="*60)
        print("PHASE 2 — Keyword search (Bing / Google)")
        print("="*60)
        search_leads = run(args) or []
        try:
            from app.functions.models import dedupe_leads as _dd
        except ModuleNotFoundError:
            from functions.models import dedupe_leads as _dd
        leads = _dd(leads + search_leads)

    if args.no_firebase:
        print("  [firebase] skipped (--no-firebase).")
    elif leads:
        print("  [firebase] running end-of-run sync to catch any missed leads...")
        push_to_firebase(leads, collection=args.firebase_collection)
    else:
        print("  [firebase] no leads to upload.")


if __name__ == "__main__":
    main()
