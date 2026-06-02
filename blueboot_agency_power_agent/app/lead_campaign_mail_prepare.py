"""lead_campaign_mail_prepare.py — Prepare outbound mail for a leads_extract campaign.

Reads the leads_extract/{extract_id} document, finds all unique countries in
leads_extracted, then writes a document per country to the
leads_extract/{extract_id}/out_mail/ subcollection.

With --prepare-contacts, also writes one personalised doc per email address to
leads_extract/{extract_id}/out_mail_contacts/{contact_id}.

Mail templates are read from mailing/leads_{extract_id}/ — scaffolded
with example files on first run.

Usage:
    python app/lead_campaign_mail_prepare.py --extract NO_high_score_may26
    python app/lead_campaign_mail_prepare.py --extract NO_high_score_may26 --prepare-contacts
    python app/lead_campaign_mail_prepare.py --extract NO_high_score_may26 --dry-run
    python app/lead_campaign_mail_prepare.py --list-extracts
"""
from __future__ import annotations

import threading as _threading

# Guards firebase_admin.initialize_app against concurrent init
_local_fb_lock = _threading.Lock()
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

LEADS_EXTRACT_COLLECTION     = "leads_extract"
MAIL_KEY_PREFIX              = "leads_"   # mailing/leads_{extract_id}/
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
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
    return firestore.client()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _catalogue_key(extract_id: str) -> str:
    """Mail catalogue folder name for this extract."""
    return f"{MAIL_KEY_PREFIX}{extract_id}"


def _list_extracts(db) -> list[str]:
    return sorted(doc.id for doc in db.collection(LEADS_EXTRACT_COLLECTION).stream())


def _load_extract_countries(db, extract_id: str) -> dict[str, dict]:
    """Return {country: {lead_count}} by reading country field from leads_extracted.

    Uses .select() so only the country + domain fields are fetched — minimal bandwidth.
    """
    extract_ref = db.collection(LEADS_EXTRACT_COLLECTION).document(extract_id)
    countries: dict[str, dict] = {}

    for lead_doc in extract_ref.collection("leads_extracted").select(["country", "domain"]).stream():
        data    = lead_doc.to_dict() or {}
        country = (data.get("country") or "?").upper()
        if country not in countries:
            countries[country] = {"lead_count": 0}
        countries[country]["lead_count"] += 1

    return countries


def _stream_leads_with_contacts(extract_ref):
    """Generator: yield (lead_doc, contact_docs_iter) for each lead.

    Streams leads fully (no .select()) so all fields are available for personalisation,
    then for each lead lazily opens its contacts_extracted subcollection.
    """
    for lead_doc in extract_ref.collection("leads_extracted").stream():
        contacts_iter = lead_doc.reference.collection("contacts_extracted").stream()
        yield lead_doc, contacts_iter


def _load_existing_mail_contacts(contacts_ref) -> dict[str, str]:
    """Pre-load all existing out_mail_contacts docs → {doc_id: status}.

    One upfront scan instead of one .get() per contact.
    """
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
    # Reject obvious placeholders and garbage
    if re.search(r"(example|test|noemail|noreply|no-reply|donotreply|invalid|localhost)", email, re.I):
        return False
    # Basic format check
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))


def _fetch_contacts_parallel(extract_ref, lead_ids: list[str], max_workers: int = 20) -> dict[str, list]:
    """Fetch contacts_extracted for all leads in parallel within the extract subtree."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def fetch_one(lead_id: str):
        docs = list(
            extract_ref.collection("leads_extracted")
                       .document(lead_id)
                       .collection("contacts_extracted")
                       .stream()
        )
        return lead_id, docs

    result: dict[str, list] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_one, lid): lid for lid in lead_ids}
        done = 0
        for fut in as_completed(futures):
            lead_id, docs = fut.result()
            result[lead_id] = docs
            done += 1
            if done % 50 == 0 or done == len(lead_ids):
                print(f"  [mail-prepare] fetched contacts: {done}/{len(lead_ids)} leads", flush=True)
    return result


def _prepare_all_contact_mails(
    db,
    extract_id:    str,
    country_tmpls: dict[str, tuple[str, str]],
    force:         bool,
    dry_run:       bool,
) -> dict[str, dict]:
    """Write out_mail_contacts docs working exclusively within leads_extract/{extract_id}/.

    All reads and writes stay inside the extract subtree:
      leads_extract/{extract_id}/leads_extracted/{lead_id}/          ← lead data
      leads_extract/{extract_id}/leads_extracted/{lead_id}/
          contacts_extracted/{contact_id}                            ← contact data
      leads_extract/{extract_id}/out_mail_contacts/{contact_id}      ← written here
    """
    extract_ref  = db.collection(LEADS_EXTRACT_COLLECTION).document(extract_id)
    contacts_ref = extract_ref.collection(OUT_MAIL_CONTACTS_COLLECTION)
    now_ts       = datetime.now(timezone.utc).isoformat()

    existing = _load_existing_mail_contacts(contacts_ref)
    counters: dict[str, dict] = {c: {"written": 0, "skipped": 0, "no_email": 0}
                                  for c in country_tmpls}

    # Step 1: load all leads within this extract
    print("  [mail-prepare] Loading leads from extract…", flush=True)
    all_leads = list(extract_ref.collection("leads_extracted").stream())
    print(f"  [mail-prepare] {len(all_leads)} leads in extract", flush=True)

    # Filter to countries we have templates for
    relevant: dict[str, dict] = {
        ld.id: ld.to_dict() or {}
        for ld in all_leads
        if (ld.to_dict() or {}).get("country", "?").upper() in country_tmpls
    }
    print(f"  [mail-prepare] {len(relevant)} leads match requested countries", flush=True)
    if not relevant:
        return counters

    # Step 2: fetch all contacts subcollections in parallel
    print("  [mail-prepare] Fetching contacts in parallel…", flush=True)
    contacts_by_lead = _fetch_contacts_parallel(extract_ref, list(relevant.keys()))

    # Step 3: process contacts and write out_mail_contacts docs
    for lead_id, lead in relevant.items():
        country = (lead.get("country") or "?").upper()
        for contact_doc in contacts_by_lead.get(lead_id, []):
            _process_contact(
                contact_doc, lead, country, country_tmpls,
                counters, existing, contacts_ref, now_ts, force, dry_run
            )

    dry_label = "  (dry-run — nothing written)" if dry_run else ""
    for country, cnt in sorted(counters.items()):
        print(
            f"  [mail-prepare] {country} → prepared: {cnt['written']}  "
            f"skipped: {cnt['skipped']}  no email: {cnt['no_email']}{dry_label}",
            flush=True,
        )
    return counters


def _process_contact(
    contact_doc, lead: dict, country: str,
    country_tmpls: dict, counters: dict, existing: dict,
    contacts_ref, now_ts: str, force: bool, dry_run: bool
) -> None:
    """Write one out_mail_contacts doc for a single contact."""
    contact = contact_doc.to_dict() or {}
    email   = (contact.get("email") or "").strip().lower()
    cnt     = counters[country]

    if not _valid_email(email):
        cnt["no_email"] += 1
        return

    existing_status = existing.get(contact_doc.id, None)
    if existing_status is not None:
        if existing_status != "pending":
            cnt["skipped"] += 1
            return
        if not force:
            cnt["skipped"] += 1
            return

    body_tmpl, subject_tmpl = country_tmpls[country]
    body, subject = personalise(body_tmpl, subject_tmpl, contact, lead)

    doc = {
        "contact_id":  contact_doc.id,
        "lead_id":     contact_doc.reference.parent.parent.id,
        "email":       email,
        "name":        contact.get("name") or "",
        "country":     country,
        "subject":     subject,
        "body":        body,
        "domain":      lead.get("domain") or "",
        "website":     lead.get("website") or "",
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


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def prepare_extract_mail(
    extract_id:        str,
    subject:           str         = "",
    subject_file:      str | None  = None,
    body_file:         str | None  = None,
    body_dir:          str | None  = None,
    dry_run:           bool        = False,
    force:             bool        = False,
    prepare_contacts:  bool        = False,
) -> dict[str, dict]:
    """Create/update out_mail documents for each country in the extract.

    If prepare_contacts=True, also writes one personalised doc per email address
    to out_mail_contacts/{contact_id} with status=pending.
    """
    fb_key = _load_secrets()
    db     = _init_firestore(fb_key)

    extract_ref  = db.collection(LEADS_EXTRACT_COLLECTION).document(extract_id)
    extract_snap = extract_ref.get()
    if not extract_snap.exists:
        raise ValueError(f"Extract not found: {extract_id}")

    extract_data = extract_snap.to_dict() or {}
    print(f"\n  [mail-prepare] Extract: {extract_id}")
    print(f"  [mail-prepare] Leads: {extract_data.get('lead_count', '?')}  "
          f"Contacts: {extract_data.get('contact_count', '?')}")

    # Single scan of leads — collect countries AND optionally build contact mail docs
    # This avoids a second full scan of leads_extracted when --prepare-contacts is used.
    print("  [mail-prepare] Scanning leads in extract…")
    countries = _load_extract_countries(db, extract_id)

    if not countries:
        print("  [mail-prepare] No leads/countries found in extract.")
        return {}

    print(f"  [mail-prepare] Countries found: {', '.join(sorted(countries))}  "
          f"({sum(c['lead_count'] for c in countries.values())} leads total)")

    # Auto-resolve mail catalogue paths
    cat_key             = _catalogue_key(extract_id)
    effective_body_dir  = body_dir   or str(mail_dir(cat_key))
    effective_subj_file = subject_file or str(subject_file_path(cat_key))

    # Scaffold if not present (always, even on --dry-run)
    scaffold_mail_catalogue(cat_key, list(countries.keys()))

    # Load subject map
    subject_map: dict[str, str] = {}
    p = Path(effective_subj_file)
    if p.exists():
        subject_map = json.loads(p.read_text(encoding="utf-8"))
        print(f"  [mail-prepare] Subject file: {p}  ({len(subject_map)} entries)")
    else:
        print(f"  [mail-prepare] WARNING: subject file not found: {effective_subj_file}")

    out_mail_ref = extract_ref.collection(OUT_MAIL_COLLECTION)
    results: dict[str, dict] = {}
    now_ts = datetime.now(timezone.utc).isoformat()

    for country, counts in sorted(countries.items()):
        if not force:
            existing = out_mail_ref.document(country).get()
            if existing.exists:
                print(f"  [mail-prepare] SKIP {country} — already prepared (use --force)")
                results[country] = existing.to_dict() or {}
                continue

        body = resolve_body(country, body_file, effective_body_dir)
        subj = resolve_subject(country, subject, subject_map)

        if not body:
            print(f"  [mail-prepare] WARNING: no body for {country}")
        if not subj:
            print(f"  [mail-prepare] WARNING: no subject for {country}")

        doc = {
            "country":     country,
            "subject":     subj,
            "body":        body,
            "lead_count":  counts["lead_count"],
            "prepared_at": now_ts,
        }
        results[country] = doc

        print(
            f"  [mail-prepare] {country}  leads={counts['lead_count']}  "
            f"subject={subj!r:.50}  body={len(body)} chars"
        )

        if not dry_run:
            out_mail_ref.document(country).set(doc, merge=True)

    if dry_run:
        print("\n  [mail-prepare] (dry-run — nothing written to Firestore)")
    else:
        print(f"\n  [mail-prepare] Written {len(results)} out_mail docs "
              f"→ leads_extract/{extract_id}/out_mail/")

    # ── Per-contact personalised docs (single pass over all leads) ─────────
    if prepare_contacts:
        print(f"\n  [mail-prepare] Preparing per-contact mail docs…")
        country_tmpls: dict[str, tuple[str, str]] = {}
        for country in sorted(countries):
            ctmpl = resolve_body(country, body_file, effective_body_dir)
            stmpl = resolve_subject(country, subject, subject_map)
            if not ctmpl:
                print(f"  [mail-prepare] SKIP contact mails for {country} — no body")
                continue
            country_tmpls[country] = (ctmpl, stmpl)

        if country_tmpls:
            try:
                _prepare_all_contact_mails(db, extract_id, country_tmpls, force, dry_run)
            except Exception as exc:
                import traceback
                print(f"  [mail-prepare] ERROR preparing contacts: {exc}", flush=True)
                traceback.print_exc()

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
        description="Prepare outbound mail config for a leads_extract campaign"
    )
    p.add_argument("--extract",          default=None, metavar="NAME",
                   help="Extract ID under leads_extract/")
    p.add_argument("--subject",          default="", metavar="TEXT",
                   help="Default subject for all countries")
    p.add_argument("--subject-file",     default=None, metavar="FILE",
                   help='Override subject JSON  (default: mailing/leads_{extract}/subject.json)')
    p.add_argument("--body-file",        default=None, metavar="FILE",
                   help="Single body file for all countries")
    p.add_argument("--body-dir",         default=None, metavar="DIR",
                   help="Override body dir  (default: mailing/leads_{extract}/mails/)")
    p.add_argument("--prepare-contacts", action="store_true",
                   help="Write one personalised doc per email to out_mail_contacts/")
    p.add_argument("--dry-run",          action="store_true",
                   help="Skip Firestore writes (mail files still created if missing)")
    p.add_argument("--force",            action="store_true",
                   help="Re-prepare pending docs; never touches sent/failed")
    p.add_argument("--list-extracts",    action="store_true",
                   help="List all available extract IDs and exit")

    args = p.parse_args(argv)

    if args.list_extracts:
        fb_key = _load_secrets()
        db     = _init_firestore(fb_key)
        ids    = _list_extracts(db)
        if ids:
            print("Available extracts:")
            for eid in ids:
                print(f"  {eid}")
        else:
            print("No extracts found.")
        return

    if not args.extract:
        p.error("--extract is required (or use --list-extracts)")

    import traceback
    try:
        prepare_extract_mail(
            extract_id       = args.extract,
            subject          = args.subject,
            subject_file     = args.subject_file,
            body_file        = args.body_file,
            body_dir         = args.body_dir,
            dry_run          = args.dry_run,
            force            = args.force,
            prepare_contacts = args.prepare_contacts,
        )
    except Exception:
        traceback.print_exc()


if __name__ == "__main__":
    main()
