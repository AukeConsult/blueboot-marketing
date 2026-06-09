"""
campaign_sync_lib.py -- Sync a campaign between its Google Drive sheet and Firestore.

Rule
----
* Sheet does not exist → create it and dump all DB contacts (delegates to
  campaign_export_lib.run_campaign_export).
* Sheet exists:
  - Sheet wins for every field, including any new columns added manually.
  - New DB contacts that have no matching Doc ID row in the sheet are
    appended as new rows so the sheet stays complete.

Column mapping
--------------
Known columns come from CONTACT_COLUMNS (see campaign_export_lib).
Any extra / unknown column header is converted to a snake_case field name
and written straight to Firestore – new sheet columns are added to DB
automatically.
"""
from __future__ import annotations

from datetime import datetime, timezone
from collections import Counter

CAMPAIGNS_COLLECTION = "campaigns"
CONTACTS_SUBCOLLECTION = "campaign_contacts"

TAB_FOLLOWUP = "Follow up"

# Canonical header → Firestore field mapping (same as export_lib.CONTACT_COLUMNS).
_HEADER_TO_FIELD: dict[str, str] = {
    "Status":               "status",
    "Name":                 "name",
    "Email":                "email",
    "Title":                "title",
    "Website":              "website",
    "Sent at":              "sent_at",
    "Follow-up date":       "followup_date",
    "Follow-up status":     "followup_status",
    "Follow-up importance": "followup_importance",
    "Comment":               "followup_comment",
    "Lead ID":              "lead_id",
    "Doc ID":               "doc_id",
}

# Firestore field → sheet header (reverse, for building new sheet rows).
_FIELD_TO_HEADER: dict[str, str] = {v: k for k, v in _HEADER_TO_FIELD.items()}

# Fields that are always DB-controlled — sheet values are never written back.
DB_CONTROLLED = {"status", "sent_at"}


def _header_to_field(label: str) -> str:
    """Map a sheet column header to a Firestore field name."""
    if label in _HEADER_TO_FIELD:
        return _HEADER_TO_FIELD[label]
    return label.lower().replace(" ", "_")


def _quote(tab: str) -> str:
    return "'" + tab.replace("'", "''") + "'"


def _read_followup_tab(svc, sheet_id: str) -> tuple[list[str], list[dict]]:
    """Return (headers, rows) where each row is {header: value}."""
    res = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=_quote(TAB_FOLLOWUP)
    ).execute()
    raw = res.get("values", [])
    if not raw:
        return [], []
    headers = raw[0]
    rows = []
    for r in raw[1:]:
        padded = r + [""] * (len(headers) - len(r))
        rows.append({headers[i]: padded[i] for i in range(len(headers))})
    return headers, rows


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return str(v)


def run_campaign_sync(db, svc, gd, campaign_id: str, **_kwargs) -> dict:
    """Sync campaign contacts between the campaign Drive sheet and Firestore.

    If the Drive sheet does not exist the function delegates to
    run_campaign_export (creates the sheet and dumps all DB data).

    Parameters
    ----------
    db           Firestore client
    svc          Google Sheets API service
    gd           GdiskInterface instance
    campaign_id  The campaign to sync
    """
    if not campaign_id:
        raise ValueError("campaign_id is required")

    # ── 0. Guard: only sync editable campaigns ─────────────────────────────
    camp_doc = db.collection(CAMPAIGNS_COLLECTION).document(campaign_id).get()
    if camp_doc.exists:
        camp_status = (camp_doc.to_dict() or {}).get("status", "draft")
        if camp_status in ("sent", "cancelled"):
            return {
                "blocked": True,
                "reason": f"Campaign status is '{camp_status}' — sync only allowed for non-sent campaigns.",
            }

    # ── 1. Does the sheet exist? ────────────────────────────────────────────
    sheet_id = gd.find_file(campaign_id)
    if not sheet_id:
        print(f"[campaign-sync] No sheet found for '{campaign_id}' — running export to create it.", flush=True)
        from crm.campaign_export_lib import run_campaign_export
        result = run_campaign_export(db, svc, gd, campaign_id)
        result["source"] = "export (sheet created)"
        return result

    # ── 2. Read Follow up tab ───────────────────────────────────────────────
    print(f"[campaign-sync] Reading sheet '{campaign_id}' ({sheet_id})", flush=True)
    headers, sheet_rows = _read_followup_tab(svc, sheet_id)

    if not headers or not sheet_rows:
        print(f"[campaign-sync] Sheet is empty — running export to populate it.", flush=True)
        from crm.campaign_export_lib import run_campaign_export
        result = run_campaign_export(db, svc, gd, campaign_id)
        result["source"] = "export (sheet was empty)"
        return result

    doc_id_header = next((h for h in headers if _header_to_field(h) == "doc_id"), None)
    if not doc_id_header:
        raise ValueError("Sheet 'Follow up' tab has no 'Doc ID' column — cannot sync.")

    # ── 3. Build sheet index by doc_id ──────────────────────────────────────
    sheet_by_doc: dict[str, dict] = {}
    for row in sheet_rows:
        did = row.get(doc_id_header, "").strip()
        if did:
            sheet_by_doc[did] = row

    print(f"[campaign-sync] Sheet has {len(sheet_by_doc)} rows with Doc ID", flush=True)

    # ── 4. Load all DB contacts ─────────────────────────────────────────────
    contacts_col = (
        db.collection(CAMPAIGNS_COLLECTION)
        .document(campaign_id)
        .collection(CONTACTS_SUBCOLLECTION)
    )
    db_contacts: dict[str, dict] = {d.id: d.to_dict() or {} for d in contacts_col.stream()}
    print(f"[campaign-sync] DB has {len(db_contacts)} contacts", flush=True)

    # ── 5. Sheet → DB: sheet wins for every field ──────────────────────────
    BATCH_SIZE = 400
    now = datetime.now(timezone.utc).isoformat()
    updated = 0
    pairs = []

    for doc_id, sheet_row in sheet_by_doc.items():
        if doc_id not in db_contacts:
            continue   # do not add new rows from the sheet — only update existing
        update: dict = {"doc_id": doc_id, "synced_at": now}
        for header, raw_val in sheet_row.items():
            field = _header_to_field(header)
            if field == "doc_id" or field in DB_CONTROLLED:
                continue
            update[field] = raw_val

        pairs.append((doc_id, update))

    for i in range(0, len(pairs), BATCH_SIZE):
        batch = db.batch()
        for doc_id, data in pairs[i:i + BATCH_SIZE]:
            # Use update() so ArrayUnion in comment_history is applied correctly.
            batch.update(contacts_col.document(doc_id), data)
        batch.commit()
        updated += len(pairs[i:i + BATCH_SIZE])
        print(f"[campaign-sync]   written {updated}/{len(pairs)} contacts to DB", flush=True)

    # ── 6. Remove sheet rows whose doc_id is no longer in DB ────────────────
    appended = 0
    stale_doc_ids = [did for did in sheet_by_doc if did not in db_contacts]
    if stale_doc_ids:
        print(f"[campaign-sync] Removing {len(stale_doc_ids)} stale rows from sheet", flush=True)
        # Re-export the full sheet from DB — cleanest way to remove stale rows.
        # (Row-by-row deletion in Sheets API requires fragile index tracking.)
        from crm.campaign_export_lib import run_campaign_export
        run_campaign_export(db, svc, gd, campaign_id)
        print(f"[campaign-sync] Sheet regenerated after removing stale rows", flush=True)

    # ── 7. Update campaign-level stats in DB ────────────────────────────────
    all_statuses = Counter(
        row.get("Status", "pending") or "pending" for row in sheet_rows
    )
    db.collection(CAMPAIGNS_COLLECTION).document(campaign_id).update({
        "contact_count":    len(db_contacts),
        "status_breakdown": dict(all_statuses),
        "updated_at":       now,
    })

    result = {
        "campaign_id":              campaign_id,
        "sheet_id":                 sheet_id,
        "source":                   "sheet",
        "contacts_updated_in_db":   updated,
        "contacts_appended_to_sheet": appended,
    }
    return result
