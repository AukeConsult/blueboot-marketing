"""Push leads + contacts to Firestore.

Structure:
  leads/{lead_id}                 — one doc per agency, merged on upsert
  leads/{lead_id}/contacts/{id}   — one doc per email address, merged on upsert

Credentials: blueboot_secrets.fireBaseAdminKey (project root)
  or FIREBASE_CREDENTIALS env var / config/serviceAccountKey.json fallback.
Collection root: 'leads' (override with FIRESTORE_COLLECTION env var).
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from models import lead_id_from_url

if TYPE_CHECKING:
    from models import Lead


def _lead_id(website: str) -> str:
    return lead_id_from_url(website)


def _contact_id(email: str) -> str:
    return hashlib.sha1(email.lower().encode()).hexdigest()[:10]


def _parse_contacts(lead: "Lead") -> list[dict]:
    """Return a list of contact dicts parsed from the lead's emails/email_titles fields."""
    emails = [e.strip() for e in lead.emails.split(",") if e.strip()] if lead.emails else []
    titles = [t.strip() for t in lead.email_titles.split(",")] if lead.email_titles else []
    contacts = []
    for i, email in enumerate(emails):
        contacts.append({
            "email":   email,
            "title":   titles[i] if i < len(titles) else "",
            "lead_id": _lead_id(lead.website),
            "company": lead.company,
            "domain":  lead.domain,
            "website": lead.website,
            "country": lead.country_name,
            "phones":  lead.phones,
            "linkedin": lead.linkedin,
        })
    return contacts


def _get_credentials():
    try:
        import firebase_admin.credentials as fb_creds
    except ImportError:
        print("  [firebase] firebase-admin not installed — run: pip install firebase-admin")
        return None

    # 1. blueboot_secrets.py in project root
    secrets_path = Path(__file__).parent.parent / "blueboot_secrets.py"
    if secrets_path.exists():
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("blueboot_secrets", secrets_path)
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            key_dict = getattr(mod, "fireBaseAdminKey", None)
            if key_dict:
                return fb_creds.Certificate(key_dict)
        except Exception as e:
            print(f"  [firebase] could not load blueboot_secrets: {e}")

    # 2. JSON file fallback
    creds_path = os.getenv("FIREBASE_CREDENTIALS", "config/serviceAccountKey.json")
    if Path(creds_path).exists():
        return fb_creds.Certificate(creds_path)

    print("  [firebase] no credentials found — skipping sync.")
    return None


def _get_db(collection: str | None = None):
    """Return (db, col, col_name) — initialises Firebase lazily, cached after first call."""
    try:
        import firebase_admin
        from firebase_admin import firestore
    except ImportError:
        return None, None, None

    cred = _get_credentials()
    if cred is None:
        return None, None, None

    col_name = collection or os.getenv("FIRESTORE_COLLECTION", "leads")
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)

    db  = firestore.client()
    col = db.collection(col_name)
    return db, col, col_name


def upsert_lead(lead: "Lead", collection: str | None = None) -> None:
    """Write a single lead + its contacts to Firestore immediately.

    Called right after each site is scraped so data lands in Firebase
    as soon as it is available — no need to wait for the full run to finish.
    """
    from dataclasses import asdict
    db, col, col_name = _get_db(collection)
    if col is None:
        return

    lid      = _lead_id(lead.website)
    lead_doc = asdict(lead)
    lead_doc["lead_id"] = lid
    lead_doc.pop("emails",       None)
    lead_doc.pop("email_titles", None)
    lead_doc.pop("email_phones", None)
    lead_doc.pop("email_names",  None)

    col.document(lid).set(lead_doc, merge=True)

    emails     = [e.strip() for e in lead.emails.split(",")       if e.strip()] if lead.emails       else []
    titles     = [t.strip() for t in lead.email_titles.split(",") if True]      if lead.email_titles else []
    per_phones = [p.strip() for p in lead.email_phones.split(",") if True]      if lead.email_phones else []
    per_names  = [n.strip() for n in lead.email_names.split(",")  if True]      if lead.email_names  else []

    contacts_col = col.document(lid).collection("contacts")
    for i, email in enumerate(emails):
        cid     = _contact_id(email)
        contact = {
            "email":   email,
            "name":    per_names[i]  if i < len(per_names)  else "",
            "title":   titles[i]     if i < len(titles)     else "",
            "phone":   per_phones[i] if i < len(per_phones) else "",
            "lead_id": lid,
            "company": lead.company,
            "domain":  lead.domain,
            "website": lead.website,
            "country": lead.country_name,
            "linkedin": lead.linkedin,
        }
        contacts_col.document(cid).set(contact, merge=True)


def sync_leads(leads: list["Lead"]) -> None:
    """Upsert leads and their contacts into Firestore (merge=True on both levels)."""
    try:
        import firebase_admin
        from firebase_admin import firestore
    except ImportError:
        print("  [firebase] firebase-admin not installed -- run: pip install firebase-admin")
        return

    cred = _get_credentials()
    if cred is None:
        return

    collection = os.getenv("FIRESTORE_COLLECTION", "leads")
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)

    db  = firestore.client()
    col = db.collection(collection)
    MAX_BATCH = 400
    batch      = db.batch()
    ops        = 0
    lead_count = 0
    contact_count = 0

    def _flush():
        nonlocal batch, ops
        if ops:
            batch.commit()
        batch = db.batch()
        ops = 0

    for lead in leads:
        lead_id  = _lead_id(lead.website)
        lead_doc = asdict(lead)
        lead_doc["lead_id"] = lead_id
        lead_doc.pop("emails",       None)
        lead_doc.pop("email_titles", None)
        lead_doc.pop("email_phones", None)
        lead_doc.pop("email_names",  None)
        batch.set(col.document(lead_id), lead_doc, merge=True)
        ops += 1
        lead_count += 1
        for contact in _parse_contacts(lead):
            cid = _contact_id(contact["email"])
            batch.set(col.document(lead_id).collection("contacts").document(cid),
                      contact, merge=True)
            ops += 1
            contact_count += 1
        if ops >= MAX_BATCH:
            _flush()
    _flush()
    print(f"  [firebase] synced {lead_count} leads + {contact_count} contacts -> {collection}")
