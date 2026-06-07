"""
sync_auth_users.py -- One-time (or periodic) sync of all Firebase Auth users
                      into the Firestore user-mirror collection.

Firestore path: settings/users/users/{normalizedEmail}

Fields written:
  uid, email, displayName, photoURL, providers[], createdAt, updatedAt

Fields PRESERVED (not overwritten if already set):
  role, notes

Run from the project root:
  python app/sync_auth_users.py
  python app/sync_auth_users.py --dry-run   # print only, no writes
"""

import _pathsetup  # noqa — adds app/ and project root to sys.path

import argparse
from datetime import datetime, timezone

import firebase_admin
from firebase_admin import auth as fb_auth

from app.firestore_client import get_firestore

USERS_COLL = ("settings", "users", "users")   # collection path segments


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def user_to_doc(user: fb_auth.UserRecord, now: str) -> dict:
    return {
        "uid":         user.uid,
        "email":       user.email or "",
        "displayName": user.display_name or "",
        "photoURL":    user.photo_url or "",
        "providers":   [p.provider_id for p in (user.provider_data or [])],
        "createdAt":   datetime.fromtimestamp(
                           user.user_metadata.creation_timestamp / 1000,
                           tz=timezone.utc
                       ).isoformat() if user.user_metadata.creation_timestamp else now,
        "updatedAt":   now,
    }


def sync(dry_run: bool = False):
    db  = get_firestore()
    now = datetime.now(timezone.utc).isoformat()

    col_ref = db.collection(USERS_COLL[0]).document(USERS_COLL[1]).collection(USERS_COLL[2])

    created = updated = skipped = 0
    page = fb_auth.list_users()

    while page:
        for user in page.users:
            key = normalize_email(user.email)
            if not key:
                print(f"  SKIP (no email): uid={user.uid}")
                skipped += 1
                continue

            doc_ref  = col_ref.document(key)
            existing = doc_ref.get()
            new_data = user_to_doc(user, now)

            if existing.exists:
                kept = existing.to_dict()
                # Preserve role and notes — don't overwrite admin assignments
                for field in ("role", "notes"):
                    if kept.get(field):
                        new_data[field] = kept[field]
                if not dry_run:
                    doc_ref.set(new_data, merge=True)
                role_info = f"  (role kept: {kept.get('role', '—')})" if kept.get("role") else ""
                print(f"  UPDATE {key}{role_info}")
                updated += 1
            else:
                if not dry_run:
                    doc_ref.set(new_data)
                print(f"  CREATE {key}")
                created += 1

        page = page.get_next_page()

    print()
    print(f"Done — created: {created}  updated: {updated}  skipped: {skipped}")
    if dry_run:
        print("(dry-run — no writes made)")


def main():
    parser = argparse.ArgumentParser(description="Sync all Firebase Auth users to Firestore")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing")
    args = parser.parse_args()

    print(f"Syncing Firebase Auth → Firestore  {'[DRY RUN] ' if args.dry_run else ''}...")
    sync(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
