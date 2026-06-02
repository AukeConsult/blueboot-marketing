"""site_campaign_mail_prepare.py — Prepare outbound mail config for a site campaign.

Reads the site_campaigns/{campaign} document, finds all unique countries in
site_campaign_sites, then writes a document per country to the
site_campaigns/{campaign}/out_mail/ subcollection.

Each out_mail/{country} document contains:
  country     — ISO country code
  subject     — email subject line
  body        — email body text (HTML or plain)
  contact_count — number of contacts in this country
  site_count    — number of sites in this country
  prepared_at   — ISO timestamp

Body and subject can be supplied via:
  --body-dir DIR       folder with files named body_NO.txt, body_SE.txt, body.txt (fallback)
  --body-file FILE     single body file used for all countries
  --subject TEXT       default subject for all countries
  --subject-file FILE  JSON file mapping country codes to subjects, e.g.:
                       {"NO": "Hei fra BlueSearch", "SE": "Hej från BlueSearch"}

Usage:
    python app/site_campaign_mail_prepare.py --campaign NO_jun01 --subject "Hello from BlueSearch" --body-file mail/body.html
    python app/site_campaign_mail_prepare.py --campaign NO_jun01 --body-dir mail/bodies --subject-file mail/subjects.json
    python app/site_campaign_mail_prepare.py --campaign NO_jun01 --dry-run
    python app/site_campaign_mail_prepare.py --list-campaigns
"""
from __future__ import annotations

import threading as _threading
import argparse
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import _pathsetup  # noqa: F401
from _mail_utils import (
    mail_dir, subject_file_path, scaffold_mail_catalogue,
    resolve_body, resolve_subject, personalise,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SITE_CAMPAIGNS_COLLECTION = "site_campaigns"
MAIL_CATALOGUE_DIR        = "mailing"  # relative to project root
OUT_MAIL_COLLECTION          = "out_mail"
OUT_MAIL_CONTACTS_COLLECTION = "out_mail_contacts"

# ---------------------------------------------------------------------------
# Firestore init
# ---------------------------------------------------------------------------

def _load_secrets():
    secrets_path = Path(__file__).parent.parent / "blueboot_secrets.py"
    if not secrets_path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("blueboot_secrets", secrets_path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, "fireBaseAdminKey", None)
    except Exception as e:
        print(f"  [mail-prepare] could not load blueboot_secrets: {e}")
        return None


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
    with _local_fb_lock:
        with _local_fb_lock:
            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred)
    return firestore.client()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _list_campaigns(db) -> list[str]:
    return sorted(doc.id for doc in db.collection(SITE_CAMPAIGNS_COLLECTION).stream())


def _load_campaign_countries(db, campaign: str) -> dict[str, dict]:
    """Return {country: {site_count}} by reading ai_country from site_campaign_sites."""
    camp_ref  = db.collection(SITE_CAMPAIGNS_COLLECTION).document(campaign)
    countries: dict[str, dict] = {}
    for site_doc in camp_ref.collection("site_campaign_sites").select(["ai_country", "country"]).stream():
        data    = site_doc.to_dict() or {}
        country = (data.get("ai_country") or data.get("country") or "?").upper()
        if country not in countries:
            countries[country] = {"site_count": 0}
        countries[country]["site_count"] += 1
    return countries


def _load_existing_mail_contacts(contacts_ref) -> dict[str, str]:
    """Pre-load all existing out_mail_contacts docs → {doc_id: status}."""
    print("  [mail-prepare] Pre-loading existing out_mail_contacts…", flush=True)
    existing: dict[str, str] = {}
    for doc in contacts_ref.select(["status"]).stream():
        existing[doc.id] = (doc.to_dict() or {}).get("status", "")
    print(f"  [mail-prepare] {len(existing)} existing mail docs loaded", flush=True)
    return existing



def _valid_email(email: str) -> bool:
    """Return True if email looks like a real address."""
    import re
    if not email or "@" not in email:
        return False
    local, _, domain = email.partition("@")
    if not local or not domain or "." not in domain:
        return False
    if re.search(r"(example|test|noemail|noreply|no-reply|donotreply|invalid|localhost)", email, re.I):
        return False
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))


def _fetch_contacts_parallel(camp_ref, site_ids: list[str], max_workers: int = 20) -> dict[str, list]:
    """Fetch site_campaign_contacts for all sites in parallel within the campaign subtree."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def fetch_one(site_id: str):
        docs = list(
            camp_ref.collection("site_campaign_sites")
                    .document(site_id)
                    .collection("site_campaign_contacts")
                    .stream()
        )
        return site_id, docs

    result: dict[str, list] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_one, sid): sid for sid in site_ids}
        done = 0
        for fut in as_completed(futures):
            site_id, docs = fut.result()
            result[site_id] = docs
            done += 1
            if done % 50 == 0 or done == len(site_ids):
                print(f"  [mail-prepare] fetched contacts: {done}/{len(site_ids)} sites", flush=True)
    return result


def _prepare_all_contact_mails(
    db,
    campaign:      str,
    country_tmpls: dict[str, tuple[str, str]],
    force:         bool,
    dry_run:       bool,
) -> dict[str, dict]:
    """Write out_mail_contacts docs working exclusively within site_campaigns/{campaign}/.

    All reads and writes stay inside the campaign subtree:
      site_campaigns/{campaign}/site_campaign_sites/{site_id}/     ← site data
          site_campaign_contacts/{contact_id}                      ← contact data
      site_campaigns/{campaign}/out_mail_contacts/{contact_id}     ← written here
    """
    camp_ref     = db.collection(SITE_CAMPAIGNS_COLLECTION).document(campaign)
    contacts_ref = camp_ref.collection(OUT_MAIL_CONTACTS_COLLECTION)
    now_ts       = datetime.now(timezone.utc).isoformat()

    existing = _load_existing_mail_contacts(contacts_ref)
    counters: dict[str, dict] = {c: {"written": 0, "skipped": 0, "no_email": 0}
                                  for c in country_tmpls}

    # Step 1: load all sites within this campaign
    print("  [mail-prepare] Loading sites from campaign…", flush=True)
    all_sites = list(camp_ref.collection("site_campaign_sites").stream())
    print(f"  [mail-prepare] {len(all_sites)} sites in campaign", flush=True)

    # Filter to countries we have templates for
    relevant: dict[str, dict] = {
        sd.id: sd.to_dict() or {}
        for sd in all_sites
        if (sd.to_dict() or {}).get("ai_country",
            sd.to_dict() or {}.get("country", "?")).upper() in country_tmpls
    }
    print(f"  [mail-prepare] {len(relevant)} sites match requested countries", flush=True)
    if not relevant:
        return counters

    # Step 2: fetch all contacts subcollections in parallel
    print("  [mail-prepare] Fetching contacts in parallel…", flush=True)
    contacts_by_site = _fetch_contacts_parallel(camp_ref, list(relevant.keys()))

    # Step 3: process contacts and write out_mail_contacts docs
    for site_id, site in relevant.items():
        country = (site.get("ai_country") or site.get("country") or "?").upper()
        if country not in country_tmpls:
            continue
        body_tmpl, subject_tmpl = country_tmpls[country]
        for contact_doc in contacts_by_site.get(site_id, []):
            contact = contact_doc.to_dict() or {}
            email   = (contact.get("email") or "").strip().lower()
            cnt     = counters[country]

            if not _valid_email(email):
                cnt["no_email"] += 1
                continue

            existing_status = existing.get(contact_doc.id, None)
            if existing_status is not None:
                if existing_status != "pending":
                    cnt["skipped"] += 1
                    continue
                if not force:
                    cnt["skipped"] += 1
                    continue

            body, subject = personalise(body_tmpl, subject_tmpl, contact, site)
            doc = {
                "contact_id":  contact_doc.id,
                "site_doc_id": site_id,
                "email":       email,
                "name":        contact.get("name") or "",
                "country":     country,
                "subject":     subject,
                "body":        body,
                "domain":      contact.get("domain") or site.get("domain") or "",
                "status":      "pending",
                "sent_at":     "",
                "prepared_at": now_ts,
            }
            if not dry_run:
                contacts_ref.document(contact_doc.id).set(doc, merge=True)
                existing[contact_doc.id] = "pending"

            cnt["written"] += 1
            total = sum(c["written"] for c in counters.values())
            if total % 100 == 0:
                label = " (dry-run)" if dry_run else ""
                print(f"  [mail-prepare] {total} contact mails prepared{label}…", flush=True)

    dry_label = "  (dry-run — nothing written)" if dry_run else ""
    for country, cnt in sorted(counters.items()):
        print(
            f"  [mail-prepare] {country} → prepared: {cnt['written']}  "
            f"skipped: {cnt['skipped']}  no email: {cnt['no_email']}{dry_label}",
            flush=True,
        )
    return counters


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def prepare_campaign_mail(
    campaign:          str,
    subject:           str         = "",
    subject_file:      str | None  = None,
    body_file:         str | None  = None,
    body_dir:          str | None  = None,
    dry_run:           bool        = False,
    force:             bool        = False,
    prepare_contacts:  bool        = False,
) -> dict[str, dict]:
    """Create/update out_mail documents for each country in the campaign.

    If prepare_contacts=True, also writes one personalised doc per email address
    to out_mail_contacts/{contact_id} with status=pending.

    Returns {country: doc_data} for every country processed.
    """
    fb_key = _load_secrets()
    db     = _init_firestore(fb_key)

    # Verify campaign exists
    camp_ref  = db.collection(SITE_CAMPAIGNS_COLLECTION).document(campaign)
    camp_snap = camp_ref.get()
    if not camp_snap.exists:
        raise ValueError(f"Campaign not found: {campaign}")

    camp_data = camp_snap.to_dict() or {}
    print(f"\n  [mail-prepare] Campaign: {campaign}")
    print(f"  [mail-prepare] Sites: {camp_data.get('site_count', '?')}  "
          f"Contacts: {camp_data.get('contact_count', '?')}")

    # Scan campaign for countries first (needed for scaffolding)
    print(f"  [mail-prepare] Scanning countries in campaign…")
    countries = _load_campaign_countries(db, campaign)

    if not countries:
        print("  [mail-prepare] No sites/countries found in campaign.")
        return {}

    print(f"  [mail-prepare] Countries found: {', '.join(sorted(countries))}")

    # Auto-resolve mail catalogue paths (project-standard structure)
    effective_body_dir  = body_dir  or str(mail_dir(campaign))
    effective_subj_file = subject_file or str(subject_file_path(campaign))

    # Scaffold if catalogue doesn't exist yet
    scaffold_mail_catalogue(campaign, list(countries.keys()))

    # Load subject map
    subject_map: dict[str, str] = {}
    p = Path(effective_subj_file)
    if p.exists():
        subject_map = json.loads(p.read_text(encoding="utf-8"))
        print(f"  [mail-prepare] Subject file: {p}  ({len(subject_map)} entries)")
    else:
        print(f"  [mail-prepare] WARNING: subject file not found: {effective_subj_file}")

    out_mail_ref = camp_ref.collection(OUT_MAIL_COLLECTION)
    results: dict[str, dict] = {}
    now_ts = datetime.now(timezone.utc).isoformat()

    for country, counts in sorted(countries.items()):
        # Skip if already prepared (unless --force)
        if not force:
            existing = out_mail_ref.document(country).get()
            if existing.exists:
                print(f"  [mail-prepare] SKIP {country} — already prepared "
                      f"(use --force to overwrite)")
                results[country] = existing.to_dict() or {}
                continue

        body    = resolve_body(country, body_file, effective_body_dir)
        subj    = resolve_subject(country, subject, subject_map)

        if not body:
            print(f"  [mail-prepare] WARNING: no body found for {country} — "
                  f"use --body-file or --body-dir")
        if not subj:
            print(f"  [mail-prepare] WARNING: no subject for {country} — "
                  f"use --subject or --subject-file")

        doc = {
            "country":       country,
            "subject":       subj,
            "body":          body,
            "site_count":    counts["site_count"],
            "contact_count": counts.get("contact_count", 0),
            "prepared_at":   now_ts,
        }
        results[country] = doc

        print(
            f"  [mail-prepare] {country}  sites={counts['site_count']}  "
            f"contacts={counts.get('contact_count', 0)}  "
            f"subject={subj!r:.50}  body={len(body)} chars"
        )

        if not dry_run:
            out_mail_ref.document(country).set(doc, merge=True)

    if dry_run:
        print("\n  [mail-prepare] (dry-run — nothing written to Firestore)")
    else:
        print(f"\n  [mail-prepare] Written {len(results)} out_mail documents "
              f"→ site_campaigns/{campaign}/out_mail/")

    # ── Per-contact personalised docs (single pass) ──────────────────────────
    if prepare_contacts:
        print("\n  [mail-prepare] Preparing per-contact mail docs…")
        country_tmpls: dict[str, tuple[str, str]] = {}
        for country in sorted(countries):
            ctmpl = resolve_body(country, body_file, effective_body_dir)
            stmpl = resolve_subject(country, subject, subject_map)
            if not ctmpl:
                print(f"  [mail-prepare] SKIP contact mails for {country} — no body")
                continue
            country_tmpls[country] = (ctmpl, stmpl)

        if country_tmpls:
            _prepare_all_contact_mails(db, campaign, country_tmpls, force, dry_run)

    return results


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
        description="Prepare outbound mail config for a site campaign"
    )
    p.add_argument("--campaign",      default=None, metavar="NAME",
                   help="Campaign ID under site_campaigns/")
    p.add_argument("--subject",       default="", metavar="TEXT",
                   help="Default email subject for all countries")
    p.add_argument("--subject-file",  default=None, metavar="FILE",
                   help="Override subject JSON file (default: mailing/<campaign>/subject.json)")
    p.add_argument("--body-file",     default=None, metavar="FILE",
                   help="Single body file (HTML or plain text) used for all countries")
    p.add_argument("--body-dir",      default=None, metavar="DIR",
                   help="Override body dir (default: mailing/<campaign>/mails/)")
    p.add_argument("--dry-run",       action="store_true",
                   help="Print what would be written without touching Firestore")
    p.add_argument("--force",         action="store_true",
                   help="Overwrite out_mail docs that already exist")
    p.add_argument("--prepare-contacts", action="store_true",
                   help="Also write one personalised doc per email address to out_mail_contacts/")
    p.add_argument("--list-campaigns", action="store_true",
                   help="List all available campaigns and exit")

    args = p.parse_args(argv)

    if args.list_campaigns:
        fb_key = _load_secrets()
        db     = _init_firestore(fb_key)
        ids    = _list_campaigns(db)
        if ids:
            print("Available campaigns:")
            for cid in ids:
                print(f"  {cid}")
        else:
            print("No campaigns found.")
        return

    if not args.campaign:
        p.error("--campaign is required (or use --list-campaigns)")

    prepare_campaign_mail(
        campaign          = args.campaign,
        subject           = args.subject,
        subject_file      = args.subject_file,
        body_file         = args.body_file,
        body_dir          = args.body_dir,
        dry_run           = args.dry_run,
        force             = args.force,
        prepare_contacts  = args.prepare_contacts,
    )


if __name__ == "__main__":
    import traceback
    try:
        main()
    except Exception as e:
        traceback.print_exc()
