"""campaign_export_lib.py -- Export a campaign to a Google Sheet.

Reads campaign_leads + campaign_contacts via the shared campaign_export_data
layer, so the columns and row structure are identical to the local Excel export
produced by app/campaign_exporter.py.

Sheet tabs:
  "Leads+Contacts" -- one row per contact, lead fields repeated per row
  "Summary"        -- campaign metadata + country / platform breakdowns

Used by the crmWorker 'campaign-export' job.
"""
from __future__ import annotations

from crm.campaign_export_data import (
    COLUMNS,
    build_summary_rows,
    load_campaign_meta,
    load_rows,
)

TAB_MAIN    = "Leads+Contacts"
TAB_SUMMARY = "Summary"

# Columns that get a data-validation dropdown in the sheet
_DROPDOWNS: dict[str, list[str]] = {}   # extend here if needed in future


# ---------------------------------------------------------------------------
# Sheets helpers
# ---------------------------------------------------------------------------

def _quote(tab: str) -> str:
    return "'" + tab.replace("'", "''") + "'"


def _ensure_tabs(svc, sheet_id: str, titles: list[str]) -> None:
    meta    = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheets  = meta.get("sheets", [])
    present = {s["properties"]["title"] for s in sheets}
    requests = []
    for i, title in enumerate(titles):
        if title in present:
            continue
        first = sheets[0]["properties"] if sheets else None
        if i == 0 and first and first["title"] not in titles:
            requests.append({"updateSheetProperties": {
                "properties": {"sheetId": first["sheetId"], "title": title},
                "fields": "title"}})
        else:
            requests.append({"addSheet": {"properties": {"title": title}}})
        present.add(title)
    if requests:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={"requests": requests}).execute()


def _write_tab(svc, sheet_id: str, tab: str, rows: list[list]) -> None:
    svc.spreadsheets().values().clear(
        spreadsheetId=sheet_id, range=_quote(tab)).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=_quote(tab) + "!A1",
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()


def _sheet_id_for_tab(svc, sheet_id: str, tab_title: str) -> int | None:
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == tab_title:
            return s["properties"]["sheetId"]
    return None


def _apply_dropdown(svc, sheet_id: str, tab_sheet_id: int,
                    n_rows: int, col_idx: int, values: list[str]) -> None:
    if tab_sheet_id is None or n_rows <= 0:
        return
    req = {"setDataValidation": {
        "range": {
            "sheetId":          tab_sheet_id,
            "startRowIndex":    1,
            "endRowIndex":      1 + n_rows,
            "startColumnIndex": col_idx,
            "endColumnIndex":   col_idx + 1,
        },
        "rule": {
            "condition": {
                "type":   "ONE_OF_LIST",
                "values": [{"userEnteredValue": v} for v in values],
            },
            "showCustomUi": True,
            "strict":       False,
        },
    }}
    try:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={"requests": [req]}).execute()
    except Exception as exc:
        print(f"[campaign-export] dropdown skipped: {exc}", flush=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_campaign_export(db, svc, gd, campaign_id: str) -> dict:
    if not campaign_id:
        raise ValueError("campaign-export requires a 'campaign_id'")
    if gd is None or not gd.is_configured():
        raise ValueError("No gdisk folder configured (Settings page).")

    campaign = load_campaign_meta(db, campaign_id)
    rows     = load_rows(db, campaign_id)

    # ── Build sheet rows ──────────────────────────────────────────────────────
    headers   = [label for _key, label, _w in COLUMNS]
    main_rows: list[list] = [headers]
    for row in rows:
        main_rows.append([
            str(row.get(key, "") if row.get(key) is not None else "")
            for key, _label, _w in COLUMNS
        ])

    summary_rows = build_summary_rows(campaign_id, campaign, rows)

    # ── Write to Google Sheets ────────────────────────────────────────────────
    sheet_id = gd.ensure_sheet(campaign_id)
    _ensure_tabs(svc, sheet_id, [TAB_MAIN, TAB_SUMMARY])
    _write_tab(svc, sheet_id, TAB_MAIN,    main_rows)
    _write_tab(svc, sheet_id, TAB_SUMMARY, summary_rows)

    # Apply any dropdowns
    if _DROPDOWNS:
        tab_sheet_id = _sheet_id_for_tab(svc, sheet_id, TAB_MAIN)
        field_keys   = [key for key, _l, _w in COLUMNS]
        for field, values in _DROPDOWNS.items():
            if field in field_keys:
                _apply_dropdown(svc, sheet_id, tab_sheet_id,
                                len(rows), field_keys.index(field), values)

    lead_ids = {r["lead_id"] for r in rows}

    print(f"[campaign-export] {campaign_id} → "
          f"{len(lead_ids)} leads / {len(rows)} contacts → sheet '{campaign_id}'",
          flush=True)

    return {
        "campaign_id": campaign_id,
        "sheet_id":    sheet_id,
        "sheet_name":  campaign_id,
        "tabs":        [TAB_MAIN, TAB_SUMMARY],
        "leads":       len(lead_ids),
        "contacts":    len(rows),
        "url":         f"https://docs.google.com/spreadsheets/d/{sheet_id}",
    }
