"""
contact_sync.py -- CLI wrapper for contact_sync_lib.

Logic lives in functions-crm/crm/contact_sync_lib.py (single source of truth).
This script sets up local auth (Firestore + OAuth2 Sheets token) and calls the lib.

Usage:
    python crm/contact_sync.py --countries NO
    python crm/contact_sync.py --countries NO --max 500
    python crm/contact_sync.py --sync-back
"""
from __future__ import annotations

import sys
import threading
import argparse
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

# -- Path setup ---------------------------------------------------------------
_here = Path(__file__).resolve().parent          # crm/
_root = _here.parent                             # blueboot_agency_power_agent/
_lib  = _root / "functions-crm"                  # functions-crm/

sys.path.insert(0, str(_root / "app"))           # app/ for _pathsetup, firebase_cred etc.
sys.path.insert(0, str(_lib))                    # functions-crm/ for crm.contact_sync_lib

import _pathsetup  # noqa: F401,F811

from functions.firebase_cred import get_firebase_cred
import firebase_admin
from firebase_admin import firestore
import firebase_admin.credentials as fb_creds

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from crm.contact_sync_lib import run_contact_sync
from crm.sheets_config import CONTACT_SHEET_ID, CONTACT_TAB

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
    p = argparse.ArgumentParser(description="Export email_contacts -> contact sheet")
    p.add_argument("--countries",  nargs="+", default=None, metavar="CC")
    p.add_argument("--campaign",   default=None)
    p.add_argument("--status",     default=None)
    p.add_argument("--collection", default="email_contacts")
    p.add_argument("--tab",        default=CONTACT_TAB)
    p.add_argument("--max",        default=None, type=int)
    p.add_argument("--sync-back",  action="store_true",
                   help="Full sync: sheet -> merge with Firestore -> write back")
    args = p.parse_args()

    countries = None
    if args.countries:
        raw = []
        for t in args.countries:
            raw.extend(c.strip().upper() for c in t.split(",") if c.strip())
        countries = raw or None

    db  = _init_firestore()
    svc = _sheets_service()

    if args.sync_back:
        from crm.contact_sync_lib import run_sync_back
        run_sync_back(db=db, svc=svc, tab=args.tab)
    else:
        added = run_contact_sync(
            db=db, svc=svc,
            countries=countries,
            status=args.status,
            campaign=args.campaign,
            max_rows=args.max,
            tab=args.tab,
        )
        print(f"[contact-sync] Done -- {added} new rows added.")


if __name__ == "__main__":
    main()
