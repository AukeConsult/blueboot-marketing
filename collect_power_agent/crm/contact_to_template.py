"""
contact_to_template.py -- CLI wrapper for contact_to_template_lib.

Logic lives in functions-crm/crm/contact_to_template_lib.py.

Usage:
    python crm/contact_to_template.py
    python crm/contact_to_template.py --dry-run
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

from crm.contact_to_template_lib import run_push_selected
from crm.sheets_config import CONTACT_TAB, TEMPLATE_TAB

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
    p = argparse.ArgumentParser(description="Push selected contacts -> CRM template")
    p.add_argument("--contact-tab",  default=CONTACT_TAB)
    p.add_argument("--template-tab", default=TEMPLATE_TAB)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    db  = _init_firestore()
    svc = _sheets_service()

    added = run_push_selected(db=db, svc=svc,
                              contact_tab=args.contact_tab,
                              template_tab=args.template_tab,
                              dry_run=args.dry_run)
    print(f"[c2t] Done -- {added} sites added to CRM template.")


if __name__ == "__main__":
    main()
