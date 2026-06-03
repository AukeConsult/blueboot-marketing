"""
template_sync.py -- Sync CRM template sheet -> Firestore + update site_leads.

Usage:
    python crm/template_sync.py
    python crm/template_sync.py --dry-run
    python crm/template_sync.py --tab Outreach
"""
from __future__ import annotations

import sys
import threading
import argparse
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

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

from crm.crm_template_sync_lib import run_template_sync
from crm.sheets_config import TEMPLATE_TAB

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
    p = argparse.ArgumentParser(
        description="Sync CRM template sheet -> Firestore + update site_leads")
    p.add_argument("--tab",     default=TEMPLATE_TAB)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    db  = _init_firestore()
    svc = _sheets_service()

    count = run_template_sync(db=db, svc=svc, tab=args.tab)
    print(f"[template-sync] Done -- {count} docs synced.")


if __name__ == "__main__":
    main()
