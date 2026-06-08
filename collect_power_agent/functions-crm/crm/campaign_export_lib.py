"""campaign_export_lib.py -- Export a campaign (and its contacts) to a Google
Sheet stored in the gdisk Drive folder.

Rule: the sheet's name is ALWAYS the campaign id. Re-exporting reuses the same
sheet. The sheet has two tabs:
  * "Follow up" -- one row per contact (the working list)
  * "Summary"   -- campaign-level totals, status breakdown, action-code legend

Column ownership on the Follow up tab:
  * "Status" is BACKEND-CONTROLLED -- it is written from Firestore on every
    export and is NEVER read back from the sheet.
  * "Last action" (date) and "Last action status" are USER-EDITABLE follow-up
    fields -- their values are preserved across re-exports (matched by Doc ID),
    so re-running the export never wipes manual notes.

Used by the crmWorker 'campaign-export' job.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

CAMPAIGNS_COLLECTION = "campaigns"
CONTACTS_SUBCOLLECTION = "campaign_contacts"

TAB_FOLLOWUP = "Follow up"
TAB_SUMMARY = "Summary"

# Valid "Last action status" codes (also rendered as a dropdown on the sheet).
ACTION_CODES = [
    ("new",          "Not contacted yet"),
    ("emailed",      "Outreach email sent"),
    ("no_reply",     "Emailed, no response yet"),
    ("replied",      "Contact replied"),
    ("meeting_set",  "Meeting / call booked"),
    ("negotiating",  "In active discussion"),
    ("won",          "Converted / deal won"),
    ("lost",         "Not interested / lost"),
    ("bounced",      "Email bounced / invalid"),
    ("unsubscribed", "Opted out / asked to stop"),
    ("callback",     "Follow up later"),
]
ACTION_CODE_VALUES = [c for c, _desc in ACTION_CODES]

# (header label, contact field) -- Follow up columns, in order.
# Status/sent_at are backend-owned.
# last_action* are user-editable sheet fields (preserved on re-export).
# followup_* are managed by the CRM Follow-up page (source of truth: Firestore);
#   they are written from Firestore on every export and are NOT preserved from the sheet.
CONTACT_COLUMNS = [
    ("Status",               "status"),
    ("Name",                 "name"),
    ("Email",                "email"),
    ("Title",                "title"),
    ("Website",              "website"),
    ("Sent at",              "sent_at"),
    ("Last action",          "last_action"),
    ("Last action status",   "last_action_status"),
    ("Follow-up date",       "followup_date"),
    ("Follow-up status",     "followup_status"),
    ("Follow-up importance", "followup_importance"),
    ("Follow-up comment",    "followup_comment"),
    ("Lead ID",              "lead_id"),
    ("Doc ID",               "doc_id"),
]

# Dropdown values for the new follow-up columns.
FOLLOWUP_STATUS_VALUES     = ["open", "contacted", "replied", "meeting", "closed", "not_interested"]
FOLLOWUP_IMPORTANCE_VALUES = ["low", "medium", "high"]

# Follow up fields preserved from the existing sheet across re-exports.
PRESERVE_FIELDS = {
    "Last action":        "last_action",
    "Last action status": "last_action_status",
}


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


def _read_existing_followup(svc, sheet_id: str) -> dict:
    """Map doc_id -> {field: value} for the preserved user-editable columns."""
    try:
        res = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=_quote(TAB_FOLLOWUP)).execute()
    except Exception:
        return {}
    rows = res.get("values", [])
    if not rows:
        return {}
    header = rows[0]
    idx = {h: i for i, h in enumerate(header)}
    doc_i = idx.get("Doc ID")
    if doc_i is None:
        return {}
    out: dict = {}
    for r in rows[1:]:
        doc_id = r[doc_i] if doc_i < len(r) else ""
        if not doc_id:
            continue
        out[doc_id] = {
            field: (r[idx[label]] if (label in idx and idx[label] < len(r)) else "")
            for label, field in PRESERVE_FIELDS.items()
        }
    return out


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


def _apply_status_dropdown(svc, sheet_id: str, fu_sheet_id: int, n_contacts: int) -> None:
    """Data-validation dropdown of ACTION_CODE_VALUES on the Last action status column."""
    _apply_dropdown(svc, sheet_id, fu_sheet_id, n_contacts,
                    "last_action_status", ACTION_CODE_VALUES)


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
    contacts.sort(key=lambda c: (str(c.get("status") or ""), str(c.get("name") or "")))

    sheet_id = gd.ensure_sheet(campaign_id)
    _ensure_tabs(svc, sheet_id, [TAB_FOLLOWUP, TAB_SUMMARY])

    # Preserve user-editable follow-up fields from the current sheet (by Doc ID).
    prev = _read_existing_followup(svc, sheet_id)
    for c in contacts:
        carried = prev.get(str(c.get("doc_id") or ""), {})
        for field in PRESERVE_FIELDS.values():
            c[field] = carried.get(field, "")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status_counts = Counter(str(c.get("status") or "unknown") for c in contacts)

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
    summary_rows += [[], ["Action codes (Last action status)", "meaning"]]
    summary_rows += [[code, desc] for code, desc in ACTION_CODES]

    _write_tab(svc, sheet_id, TAB_FOLLOWUP, followup_rows)
    _write_tab(svc, sheet_id, TAB_SUMMARY, summary_rows)
    fu_sheet_id = _followup_sheet_id(svc, sheet_id)
    _apply_status_dropdown(svc, sheet_id, fu_sheet_id, len(contacts))
    _apply_dropdown(svc, sheet_id, fu_sheet_id, len(contacts),
                    "followup_status",     FOLLOWUP_STATUS_VALUES)
    _apply_dropdown(svc, sheet_id, fu_sheet_id, len(contacts),
                    "followup_importance", FOLLOWUP_IMPORTANCE_VALUES)

    return {
        "campaign_id":  campaign_id,
        "sheet_id":     sheet_id,
        "sheet_name":   campaign_id,
        "tabs":         [TAB_FOLLOWUP, TAB_SUMMARY],
        "contacts":     len(contacts),
        "by_status":    dict(status_counts),
        "action_codes": ACTION_CODE_VALUES,
        "url":          f"https://docs.google.com/spreadsheets/d/{sheet_id}",
    }
