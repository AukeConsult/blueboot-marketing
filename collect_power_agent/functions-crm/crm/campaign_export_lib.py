"""campaign_export_lib.py -- Export a campaign (and its contacts) to a Google
Sheet stored in the gdisk Drive folder.

Rule: the sheet's name is ALWAYS the campaign id. Re-exporting reuses the same
sheet. The sheet has two tabs:
  * "Follow up" -- one row per contact (the working list)
  * "Summary"   -- campaign-level totals, status breakdown, action-code legend

Column ownership on the Follow up tab:
  * "Status" is BACKEND-CONTROLLED -- it is written from Firestore on every
    export and is NEVER read back from the sheet.

Used by the crmWorker 'campaign-export' job.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

CAMPAIGNS_COLLECTION = "campaigns"
CONTACTS_SUBCOLLECTION = "campaign_contacts"

TAB_FOLLOWUP = "Follow up"
TAB_SUMMARY = "Summary"

# (header label, contact field) -- Follow up columns, in order.
# Status/sent_at are backend-owned.
# followup_* are written from Firestore on every export.
# Comment is last for easy reading.
CONTACT_COLUMNS = [
    ("Status",               "status"),
    ("Name",                 "name"),
    ("Email",                "email"),
    ("Title",                "title"),
    ("Website",              "website"),
    ("Sent at",              "sent_at"),
    ("Follow-up date",       "followup_date"),
    ("Follow-up status",     "followup_status"),
    ("Follow-up importance", "followup_importance"),
    ("Follow-up owner",      "followup_owner"),
    ("Lead ID",              "lead_id"),
    ("Doc ID",               "doc_id"),
    ("Comment",              "followup_comment"),
]

# Dropdown values for the new follow-up columns.
FOLLOWUP_STATUS_VALUES     = ["open", "contacted", "replied", "meeting", "closed", "not_interested"]
FOLLOWUP_IMPORTANCE_VALUES = ["low", "medium", "high"]
CONTACT_STATUSES = {"pending", "active", "excluded"}
LEGACY_ACTIVE_STATUSES = {"sent", "dosend", "emailed", "replied", "bounced", "error"}


def _contact_status(value) -> str:
    status = str(value or "pending").strip().lower()
    if status in CONTACT_STATUSES:
        return status
    if status in LEGACY_ACTIVE_STATUSES:
        return "active"
    return "pending"


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return str(v)


def _quote(tab: str) -> str:
    return "'" + tab.replace("'", "''") + "'"


def _ensure_tabs(svc, sheet_id: str, titles: list[str]) -> None:
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheets = meta.get("sheets", [])
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


def _followup_sheet_id(svc, sheet_id: str) -> int | None:
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == TAB_FOLLOWUP:
            return s["properties"]["sheetId"]
    return None


def _write_tab(svc, sheet_id: str, tab: str, rows: list[list]) -> None:
    svc.spreadsheets().values().clear(spreadsheetId=sheet_id, range=_quote(tab)).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=_quote(tab) + "!A1",
        valueInputOption="RAW", body={"values": rows}).execute()


def _apply_dropdown(svc, sheet_id: str, fu_sheet_id: int, n_contacts: int,
                    field: str, values: list[str]) -> None:
    """Add a data-validation dropdown to the column that maps to *field*."""
    if fu_sheet_id is None or n_contacts <= 0:
        return
    fields = [f for _l, f in CONTACT_COLUMNS]
    if field not in fields:
        return
    col = fields.index(field)
    req = {"setDataValidation": {
        "range": {"sheetId": fu_sheet_id, "startRowIndex": 1, "endRowIndex": 1 + n_contacts,
                  "startColumnIndex": col, "endColumnIndex": col + 1},
        "rule": {
            "condition": {"type": "ONE_OF_LIST",
                          "values": [{"userEnteredValue": v} for v in values]},
            "showCustomUi": True, "strict": False,
        }}}
    try:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={"requests": [req]}).execute()
    except Exception as exc:
        print(f"[campaign-export] dropdown skipped ({field}): {exc}", flush=True)


def _hide_columns(svc, sheet_id: str, fu_sheet_id: int, fields: list[str]) -> None:
    """Hide the columns for the given field names (values preserved for sync)."""
    if fu_sheet_id is None:
        return
    col_order = [f for _l, f in CONTACT_COLUMNS]
    requests = []
    for field in fields:
        if field not in col_order:
            continue
        col = col_order.index(field)
        requests.append({"updateDimensionProperties": {
            "range": {
                "sheetId":    fu_sheet_id,
                "dimension":  "COLUMNS",
                "startIndex": col,
                "endIndex":   col + 1,
            },
            "properties": {"hiddenByUser": True},
            "fields":     "hiddenByUser",
        }})
    if requests:
        try:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id, body={"requests": requests}).execute()
        except Exception as exc:
            print(f"[campaign-export] hide columns skipped: {exc}", flush=True)


def run_campaign_export(db, svc, gd, campaign_id: str) -> dict:
    if not campaign_id:
        raise ValueError("campaign-export requires a 'campaign_id'")
    if gd is None or not gd.is_configured():
        raise ValueError("No gdisk folder configured (Settings page).")

    snap = db.collection(CAMPAIGNS_COLLECTION).document(campaign_id).get()
    if not snap.exists:
        raise ValueError(f"Campaign '{campaign_id}' not found")
    camp = snap.to_dict() or {}

    contacts = [c.to_dict() or {} for c in db.collection(CAMPAIGNS_COLLECTION)
                .document(campaign_id).collection(CONTACTS_SUBCOLLECTION).stream()]
    for c in contacts:
        c["status"] = _contact_status(c.get("status"))
    contacts.sort(key=lambda c: (str(c.get("status") or ""), str(c.get("name") or "")))

    sheet_id = gd.ensure_sheet(campaign_id)
    _ensure_tabs(svc, sheet_id, [TAB_FOLLOWUP, TAB_SUMMARY])

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status_counts = Counter(c.get("status", "pending") for c in contacts)

    followup_rows = [[label for label, _f in CONTACT_COLUMNS]]
    for c in contacts:
        followup_rows.append([_cell(c.get(field)) for _label, field in CONTACT_COLUMNS])

    summary_rows = [
        ["Campaign",         campaign_id],
        ["Exported",         now],
        ["Status",           _cell(camp.get("status"))],
        ["Owner",            _cell(camp.get("owner"))],
        ["Outreach email",   _cell(camp.get("outreach_email_account"))],
        ["Sites",            _cell(camp.get("sites_count"))],
        ["Countries",        _cell(camp.get("countries"))],
        ["Updated at",       _cell(camp.get("updated_at"))],
        [],
        ["Total contacts",   len(contacts)],
        [],
        ["Contacts by status", "count"],
    ]
    for status, n in sorted(status_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        summary_rows.append([status, n])
    _write_tab(svc, sheet_id, TAB_FOLLOWUP, followup_rows)
    _write_tab(svc, sheet_id, TAB_SUMMARY, summary_rows)
    fu_sheet_id = _followup_sheet_id(svc, sheet_id)
    _apply_dropdown(svc, sheet_id, fu_sheet_id, len(contacts),
                    "followup_status",     FOLLOWUP_STATUS_VALUES)
    _apply_dropdown(svc, sheet_id, fu_sheet_id, len(contacts),
                    "followup_importance", FOLLOWUP_IMPORTANCE_VALUES)
    _hide_columns(svc, sheet_id, fu_sheet_id, ["lead_id", "doc_id"])

    return {
        "campaign_id":  campaign_id,
        "sheet_id":     sheet_id,
        "sheet_name":   campaign_id,
        "tabs":         [TAB_FOLLOWUP, TAB_SUMMARY],
        "contacts":     len(contacts),
        "by_status":    dict(status_counts),
        "url":          f"https://docs.google.com/spreadsheets/d/{sheet_id}",
    }

