"""maint_clean_contacts.py -- clean names (and report bad e-mails) on existing contacts.

Applies the same rules the scraper now uses (crm.contact_clean_lib):
  "Email Adam Kenneman."  -> "Adam Kenneman"      "Mejla" -> ""      "Contact us" -> ""
The original text is kept in `name_raw` so a change can be undone.  Only the `name` field
is changed; e-mail addresses are REPORTED only (the contact id is derived from the address).

DRY RUN BY DEFAULT -- nothing is written until you pass --apply.

Usage:
    python app/maint_clean_contacts.py                       # preview all collections
    python app/maint_clean_contacts.py --apply
    python app/maint_clean_contacts.py --campaign BS_UK      # only this campaign's contacts
    python app/maint_clean_contacts.py --only campaign       # campaign | site | email (repeatable)
    python app/maint_clean_contacts.py --all                 # also contacts without scraped_at
    python app/maint_clean_contacts.py --show 50             # list more before/after examples
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import _pathsetup  # noqa: F401

_CRM_DIR = str(Path(__file__).resolve().parent.parent / "functions-crm")
if _CRM_DIR not in sys.path:
    sys.path.insert(0, _CRM_DIR)

from crm.contact_clean_lib import clean_email, clean_name  # noqa: E402

BATCH = 400


def _get_db():
    try:
        from app.firestore_client import get_firestore
    except ImportError:
        from firestore_client import get_firestore
    return get_firestore()


def _sources(db, only: set[str], campaign: str):
    """Yield (label, stream) for each collection to clean."""
    if "campaign" in only:
        if campaign:
            yield "campaign_contacts", db.collection("campaigns").document(campaign) \
                .collection("campaign_contacts").stream()
        else:
            yield "campaign_contacts", db.collection_group("campaign_contacts").stream()
    if "site" in only and not campaign:
        yield "site_contacts", db.collection_group("site_contacts").stream()
    if "email" in only and not campaign:
        yield "email_contacts", db.collection("email_contacts").stream()


def main(argv=None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    p = argparse.ArgumentParser(description="Clean names on existing contacts. Dry run unless --apply.")
    p.add_argument("--only", action="append", choices=["campaign", "site", "email"], default=[],
                   help="Only this collection group (repeatable). Default: all three")
    p.add_argument("--campaign", default="", help="Only this campaign's campaign_contacts")
    p.add_argument("--all", action="store_true",
                   help="Also clean campaign contacts without scraped_at (may include names typed in the CRM)")
    p.add_argument("--show", type=int, default=15, help="Examples to print per collection (default 15)")
    p.add_argument("--apply", action="store_true", help="Write the changes (default: dry run)")
    args = p.parse_args(argv)
    only = set(args.only) or {"campaign", "site", "email"}

    db = _get_db()
    grand = {"seen": 0, "changed": 0, "emptied": 0, "bad_email": 0}
    for label, stream in _sources(db, only, args.campaign):
        print(f"[clean] scanning {label} ...", flush=True)
        seen = changed = emptied = skipped_manual = 0
        bad_emails: list[str] = []
        examples: list[str] = []
        batch, pending = db.batch(), 0
        for snap in stream:
            seen += 1
            if seen % 1000 == 0:
                print(f"[clean]   {label}: {seen} read, {changed} to change", flush=True)
            d = snap.to_dict() or {}
            email = d.get("email", "") or ""
            if email and clean_email(email) != email.strip().lower():
                bad_emails.append(email)
            old = d.get("name", "") or ""
            if not old:
                continue
            if label == "campaign_contacts" and not args.all and not d.get("scraped_at"):
                skipped_manual += 1
                continue
            new = clean_name(old, email)
            if new == old:
                continue
            changed += 1
            emptied += (new == "")
            if len(examples) < args.show:
                examples.append(f"   {old!r:55} -> {new!r}   [{email}]")
            if args.apply:
                upd = {"name": new}
                if not d.get("name_raw"):
                    upd["name_raw"] = old
                batch.update(snap.reference, upd)
                pending += 1
                if pending >= BATCH:
                    batch.commit()
                    batch, pending = db.batch(), 0
        if args.apply and pending:
            batch.commit()
        print(f"== {label}: {seen} contacts, {changed} name(s) "
              f"{'cleaned' if args.apply else 'would change'} ({emptied} not a name -> emptied)"
              + (f", {skipped_manual} skipped (no scraped_at; use --all)" if skipped_manual else ""),
              flush=True)
        for line in examples:
            print(line)
        if bad_emails:
            print(f"   {len(bad_emails)} e-mail address(es) look invalid (reported only), e.g. "
                  + ", ".join(bad_emails[:5]))
        grand["seen"] += seen
        grand["changed"] += changed
        grand["emptied"] += emptied
        grand["bad_email"] += len(bad_emails)

    print(f"\n{'DONE' if args.apply else 'DRY RUN'} -- {grand['seen']} contacts scanned, "
          f"{grand['changed']} name(s) {'cleaned' if args.apply else 'would be cleaned'}, "
          f"{grand['bad_email']} suspicious e-mail(s) reported."
          + ("" if args.apply else " Re-run with --apply to write."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
