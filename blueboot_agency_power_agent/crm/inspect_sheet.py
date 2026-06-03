"""Run this once to inspect/create the contacts tab."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
import _pathsetup

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SHEET_ID = "1aMglV53NiMEArjld37HN5cxliyNRGzIP2mrM4kwlupA"
TOKEN    = str(Path(__file__).parent.parent / "config" / "google_token.json")
SCOPES   = ["https://www.googleapis.com/auth/spreadsheets"]
TAB      = "contacts"

creds = Credentials.from_authorized_user_file(TOKEN, SCOPES)
if creds.expired and creds.refresh_token:
    creds.refresh(Request())

svc  = build("sheets", "v4", credentials=creds)
meta = svc.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
tabs = [s['properties']['title'] for s in meta['sheets']]
print("Existing tabs:", tabs)

if TAB not in tabs:
    svc.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": TAB}}}]}
    ).execute()
    print(f"Created tab: {TAB}")
else:
    print(f"Tab '{TAB}' already exists")

rows = svc.spreadsheets().values().get(
    spreadsheetId=SHEET_ID, range=f"{TAB}!A1:ZZ2"
).execute().get('values', [])

print("Headers:", rows[0] if rows else "(empty)")
if len(rows) > 1:
    print("Row 1:  ", rows[1])
