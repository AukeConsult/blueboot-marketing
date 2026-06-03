"""
setup_outreach_sheet.py -- Create a new Google Sheet with the outreach CRM structure.

Creates a new spreadsheet with:
  - Headers matching the outreach CRM columns (Norwegian)
  - Frozen header row
  - Status column with dropdown validation + color coding
  - Auto column widths
  - Filter row

Usage:
    python crm/setup_outreach_sheet.py
    python crm/setup_outreach_sheet.py --title "My Outreach Sheet"
"""
from __future__ import annotations

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import _pathsetup  # noqa: F401,F811

from dotenv import load_dotenv
load_dotenv()

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES        = ["https://www.googleapis.com/auth/spreadsheets",
                 "https://www.googleapis.com/auth/drive"]
TOKEN_PATH    = str(Path(__file__).resolve().parent.parent / "config" / "google_token.json")
CLIENT_SECRET = str(Path(__file__).resolve().parent.parent / "config" / "google_oauth_client.json")

# -- Columns ------------------------------------------------------------------
HEADERS = [
    "Dato lagt i",
    "Bedrift",
    "Nettside",
    "Bransje",
    "Størrelse",
    "Beslutningstaker",
    "Rolle",
    "E-post",
    "Telefon",
    "Score",
    "Status",
    "Selger",
    "Kommentar",
    "Tilbud",
]

COL_WIDTHS = {
    "Dato lagt i":      100,
    "Bedrift":          180,
    "Nettside":         200,
    "Bransje":          160,
    "Størrelse":        130,
    "Beslutningstaker": 160,
    "Rolle":            140,
    "E-post":           220,
    "Telefon":          120,
    "Score":             60,
    "Status":           130,
    "Selger":            90,
    "Kommentar":        300,
    "Tilbud":           120,
}

# Status dropdown values + background colors (hex without #)
STATUS_OPTIONS = [
    ("Ikke kontaktet",  "FFFFFF"),  # white
    ("Sendt Mail",      "CFE2FF"),  # light blue
    ("Snakket med",     "D4EDDA"),  # light green
    ("Må følges opp",   "FFF3CD"),  # light yellow
    ("Hatt møte",       "D1ECF1"),  # teal light
    ("Sendt tilbud",    "CCE5FF"),  # blue
    ("Akseptert tilbud","D4EDDA"),  # green
    ("Ikke interessert","F8D7DA"),  # light red
    ("Utkast",          "E2E3E5"),  # grey
]


# -- Auth ---------------------------------------------------------------------

def _service(scope_list):
    creds = None
    if Path(TOKEN_PATH).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, scope_list)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow  = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, scope_list)
            creds = flow.run_local_server(port=0)
        Path(TOKEN_PATH).write_text(creds.to_json())
    return build("sheets", "v4", credentials=creds)


# -- Sheet creation -----------------------------------------------------------

def _hex_to_rgb(hex_str):
    h = hex_str.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return {"red": r/255, "green": g/255, "blue": b/255}


def create_outreach_sheet(title: str = "Outreach CRM") -> str:
    svc = _service(SCOPES)

    # 1. Create spreadsheet
    print(f"[setup] Creating spreadsheet '{title}'...", flush=True)
    spreadsheet = svc.spreadsheets().create(body={
        "properties": {"title": title},
        "sheets": [{"properties": {"title": "Outreach", "gridProperties": {"frozenRowCount": 1}}}],
    }).execute()

    sheet_id     = spreadsheet["spreadsheetId"]
    tab_id       = spreadsheet["sheets"][0]["properties"]["sheetId"]
    sheet_url    = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
    print(f"[setup] Created: {sheet_url}", flush=True)

    requests = []

    # 2. Write headers
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range="Outreach!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [HEADERS]},
    ).execute()

    # 3. Header row formatting (bold, background, border)
    requests.append({
        "repeatCell": {
            "range": {"sheetId": tab_id, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": _hex_to_rgb("1F497D"),
                    "textFormat": {"bold": True, "foregroundColor": _hex_to_rgb("FFFFFF"), "fontSize": 10},
                    "verticalAlignment": "MIDDLE",
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)",
        }
    })

    # 4. Column widths
    for ci, header in enumerate(HEADERS):
        width = COL_WIDTHS.get(header, 120)
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": tab_id, "dimension": "COLUMNS",
                          "startIndex": ci, "endIndex": ci + 1},
                "properties": {"pixelSize": width},
                "fields": "pixelSize",
            }
        })

    # 5. Status dropdown (data validation)
    status_col = HEADERS.index("Status")
    requests.append({
        "setDataValidation": {
            "range": {
                "sheetId": tab_id,
                "startRowIndex": 1, "endRowIndex": 10000,
                "startColumnIndex": status_col, "endColumnIndex": status_col + 1,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": s} for s, _ in STATUS_OPTIONS],
                },
                "showCustomUi": True,
                "strict": False,
            },
        }
    })

    # 6. Conditional formatting — color each status value
    for status_val, hex_color in STATUS_OPTIONS:
        requests.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": tab_id,
                        "startRowIndex": 1, "endRowIndex": 10000,
                        "startColumnIndex": status_col, "endColumnIndex": status_col + 1,
                    }],
                    "booleanRule": {
                        "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": status_val}]},
                        "format": {"backgroundColor": _hex_to_rgb(hex_color)},
                    },
                },
                "index": 0,
            }
        })

    # 7. Auto filter
    requests.append({
        "setBasicFilter": {
            "filter": {
                "range": {
                    "sheetId": tab_id,
                    "startRowIndex": 0, "endRowIndex": 1,
                    "startColumnIndex": 0, "endColumnIndex": len(HEADERS),
                }
            }
        }
    })

    # 8. Row height for header
    requests.append({
        "updateDimensionProperties": {
            "range": {"sheetId": tab_id, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 28},
            "fields": "pixelSize",
        }
    })

    print("[setup] Applying formatting...", flush=True)
    svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": requests}
    ).execute()

    print("[setup] Done!")
    print(f"[setup] Sheet URL: {sheet_url}")
    print(f"[setup] Sheet ID:  {sheet_id}")
    return sheet_id


# -- Main ---------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Create outreach CRM Google Sheet")
    p.add_argument("--title", default="Outreach CRM", help="Spreadsheet title")
    args = p.parse_args()
    create_outreach_sheet(title=args.title)


if __name__ == "__main__":
    main()
