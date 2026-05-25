"""Entry point — argument parsing, mode dispatch, and Firebase upload."""
from __future__ import annotations

import argparse
import hashlib
import os
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import load_dotenv

from catalog_scrapers import catalog_run
from models import lead_id_from_url
from search_runner import run

if TYPE_CHECKING:
    from models import Lead


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

    if cred is None:
        print("  [firebase] no credentials found — skipping preload.")
        return set()

    col_name = collection or os.getenv("FIRESTORE_COLLECTION", "leads")
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)

    db  = firestore.client()
    col = db.collection(col_name)

    domains: set[str] = set()
    # Only fetch the 'domain' field to keep bandwidth minimal
    for doc in col.select(["domain"]).stream():
        d = doc.to_dict().get("domain", "")
        if d:
            domains.add(d.strip().lower())

    print(f"  [firebase] preloaded {len(domains)} existing domains from '{col_name}'")
    return domains


def push_to_firebase(leads: list["Lead"], collection: str | None = None) -> None:
    """Upsert leads + contacts into Firestore.

    Structure:
      leads/{lead_id}                — one doc per agency (emails/email_titles excluded)
      leads/{lead_id}/contacts/{id}  — one doc per email address

    Credentials loaded from blueboot_secrets.fireBaseAdminKey (project root),
    FIREBASE_CREDENTIALS env var, or config/serviceAccountKey.json fallback.
    Collection name: collection arg > FIRESTORE_COLLECTION env var > 'leads'.
    """
    try:
        import firebase_admin
        import firebase_admin.credentials as fb_creds
        from firebase_admin import firestore
    except ImportError:
        print("  [firebase] firebase-admin not installed — run: pip install firebase-admin")
        return

    # --- load credentials ---
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

    if cred is None:
        print("  [firebase] no credentials found — skipping upload.")
        return

    col_name = collection or os.getenv("FIRESTORE_COLLECTION", "leads")
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)

    db  = firestore.client()
    col = db.collection(col_name)

    # --- helpers ---
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
                "title":    titles[i] if i < len(titles) else "",
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

    # --- batch write (Firestore limit: 500 ops; flush at 400) ---
    MAX_BATCH     = 400
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
        "--mode", choices=["search", "catalog", "both"], default="both",
        help="search = Bing/Google keyword search; catalog = scrape directory listings; both = run catalog first, then search (default)",
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
        "--output", default="output",
        help="Output directory for the Excel file.",
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
        "--max-pages", type=int, default=int(os.getenv("MAX_PAGES", "3")),
        help="Max pages to crawl per agency website.",
    )
    parser.add_argument(
        "--max-country", type=int, default=int(os.getenv("MAX_COUNTRY", "5000")) or None,
        help="Stop a country after this many leads (0 = unlimited).",
    )
    parser.add_argument(
        "--give-up-after", type=int, default=int(os.getenv("GIVE_UP_AFTER", "10")),
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
    return parser


def main() -> None:
    load_dotenv()
    args = _build_parser().parse_args()

    # Always load already-handled domains from Firestore — never read the local CSV for this.
    # Returns an empty set if credentials are missing (run continues from scratch).
    args.preloaded_domains = load_leads_from_firebase(collection=args.firebase_collection)

    if args.mode == "catalog":
        leads = catalog_run(args)
    elif args.mode == "search":
        leads = run(args)
    else:  # "both" — catalog ALWAYS runs first (known good directory sources), then keyword search
        print("\n" + "="*60)
        print("PHASE 1 — Catalog scrape")
        print("="*60)
        leads = catalog_run(args) or []
        print("\n" + "="*60)
        print("PHASE 2 — Keyword search (Bing / Google)")
        print("="*60)
        search_leads = run(args) or []
        # Merge: run() already loaded existing leads internally, so dedupe both lists
        from models import dedupe_leads as _dd
        leads = _dd(leads + search_leads)

    # Each lead is already upserted to Firebase immediately after scraping.
    # The end-of-run bulk push is a safety net for any missed leads (e.g. runs
    # that were interrupted and resumed from the CSV).
    if args.no_firebase:
        print("  [firebase] skipped (--no-firebase).")
    elif leads:
        print("  [firebase] running end-of-run sync to catch any missed leads...")
        push_to_firebase(leads, collection=args.firebase_collection)
    else:
        print("  [firebase] no leads to upload.")


if __name__ == "__main__":
    main()
