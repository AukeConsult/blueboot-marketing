"""
sync_campaign.py -- Sync campaign data from contact sheet to Firestore.

Steps:
  1. Read contact sheet -> sync crm/contact_select/items (sheet wins)
  2. Update email_contacts.campaign (blank-only, use --force to overwrite)
  3. Create/update campaigns/{campaign_id} with statistics

Usage:
    python crm/sync_campaign.py
    python crm/sync_campaign.py --force
    python crm/sync_campaign.py --tab contacts
"""
from __future__ import annotations
import sys
import threading
import argparse
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

_here = Path(__file__).resolve().parent
_root = _here.parent
_lib  = _root / "functions-crm"
sys.path.insert(0, str(_root / "app"))
sys.path.insert(0, str(_lib))
import _pathsetup  # noqa: F401,F811

from functions.firebase_cred import get_firebase_cred
import firebase_admin
from firebase_admin import firestore
import firebase_admin.credentials as fb_creds
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from crm.campaign_sync_lib import run_campaign_sync
from crm.sheets_config import CONTACT_TAB

SCOPES        = ["https://www.googleapis.com/auth/spreadsheets"]
TOKEN_PATH    = str(_root / "config" / "google_token.json")
CLIENT_SECRET = str(_root / "config" / "google_oauth_client.json")
_fb_lock = threading.Lock()


def _init_firestore():
    cred_obj = get_firebase_cred()
    cred = cred_obj if isinstance(cred_obj, fb_creds.Base) else fb_creds.Certificate(cred_obj)
    with _fb_lock:
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
    return firestore.client()


def _sheets_service():
    creds = None
    if Path(TOKEN_PATH).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow  = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
            creds = flow.run_local_server(port=0)
        Path(TOKEN_PATH).write_text(creds.to_json())
    return build("sheets", "v4", credentials=creds)


def main():
    p = argparse.ArgumentParser(description="Sync campaign data from contact sheet to Firestore")
    p.add_argument("campaign_id", metavar="CAMPAIGN_ID",
                   help="Campaign ID to sync (e.g. NO_jun)")
    p.add_argument("--tab",   default=CONTACT_TAB, metavar="TAB")
    p.add_argument("--force", action="store_true",
                   help="Force-update email_contacts.campaign even if already set")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be updated without writing to Firestore")
    args = p.parse_args()

    db  = _init_firestore()
    svc = _sheets_service()
    result = run_campaign_sync(db=db, svc=svc, tab=args.tab,
                               force=args.force, campaign_id=args.campaign_id,
                               dry_run=args.dry_run)

    print(f"\n[sync-campaign] Summary:")
    print(f"  contact_select synced : {result['contact_select_synced']}")
    print(f"  email_contacts updated: {result['email_updated']}")
    print(f"  campaign docs upserted: {result['campaigns_upserted']}")
    if result['campaign_ids']:
        print(f"  campaigns: {', '.join(result['campaign_ids'])}")


if __name__ == "__main__":
    main()
