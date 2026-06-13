"""gdisk.py — Minimal Google Drive uploader for pipeline export scripts.

Uploads a local file into a subfolder of the configured Google Drive folder.
Uses google.auth.default() so it works automatically in Cloud Run (service
account credentials) and locally (Application Default Credentials via gcloud).

Configuration:
    GDISK_FOLDER_ID        — root Drive folder ID (same env var as the CRM backend)
    GDISK_EXPORT_SUBFOLDER — subfolder name inside GDISK_FOLDER_ID
                             (default: "pipeline-exports")

The subfolder is created automatically if it doesn't exist.

Usage:
    from functions.gdisk import upload_file
    url = upload_file("/tmp/out.xlsx", "site_prospects_NO_20260609.xlsx")
    # returns a Drive view URL, or None on failure / not configured
"""
from __future__ import annotations

import os

_MIME_XLSX       = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_MIME_FOLDER     = "application/vnd.google-apps.folder"
_SCOPES          = ["https://www.googleapis.com/auth/drive"]
_DEFAULT_SUBFOLDER = "pipeline-exports"

# Module-level cache — one auth + discovery per process
_service = None


def _get_service():
    global _service
    if _service is not None:
        return _service
    import google.auth
    from googleapiclient.discovery import build
    creds, _ = google.auth.default(scopes=_SCOPES)
    _service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _service


def _find_item(svc, name, parent_id, mime=None):
    """Return the id of *name* inside *parent_id*, or None."""
    q = "name = '{}' and '{}' in parents and trashed = false".format(name, parent_id)
    if mime:
        q += " and mimeType = '{}'".format(mime)
    res = svc.files().list(
        q=q, spaces="drive", pageSize=1, fields="files(id)",
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def _get_or_create_subfolder(svc, parent_id, name):
    """Return the id of subfolder *name* inside *parent_id*, creating it if needed."""
    existing = _find_item(svc, name, parent_id, mime=_MIME_FOLDER)
    if existing:
        return existing
    meta = {"name": name, "mimeType": _MIME_FOLDER, "parents": [parent_id]}
    created = svc.files().create(body=meta, fields="id", supportsAllDrives=True).execute()
    print("[gdisk] Created subfolder '{}'".format(name))
    return created["id"]


def upload_file(local_path, drive_name, mime=_MIME_XLSX):
    """Upload *local_path* to the pipeline-exports subfolder in Drive.

    Target path: <GDISK_FOLDER_ID> / <GDISK_EXPORT_SUBFOLDER> / <drive_name>

    Creates the subfolder and/or file if they don't exist; replaces the file
    if it does (same Drive id, preserves sharing settings).

    Returns a Drive view URL, or None when GDISK_FOLDER_ID is not set
    (silently skipped — normal for local runs).

    Never raises — logs a warning and returns None on any error so the
    caller's pipeline step is never aborted by a Drive failure.
    """
    root_id = os.environ.get("GDISK_FOLDER_ID", "").strip()
    if not root_id:
        return None  # not configured — silently skip (normal for local runs)

    subfolder_name = (
        os.environ.get("GDISK_EXPORT_SUBFOLDER", "").strip() or _DEFAULT_SUBFOLDER
    )

    try:
        from googleapiclient.http import MediaFileUpload

        svc       = _get_service()
        folder_id = _get_or_create_subfolder(svc, root_id, subfolder_name)
        media     = MediaFileUpload(str(local_path), mimetype=mime, resumable=False)

        existing_id = _find_item(svc, drive_name, folder_id)
        if existing_id:
            svc.files().update(
                fileId=existing_id,
                media_body=media,
                supportsAllDrives=True,
            ).execute()
            file_id = existing_id
            print("[gdisk] Updated  -> {}/{}  (id={})".format(subfolder_name, drive_name, file_id))
        else:
            meta = {"name": drive_name, "parents": [folder_id]}
            created = svc.files().create(
                body=meta, media_body=media, fields="id",
                supportsAllDrives=True,
            ).execute()
            file_id = created["id"]
            print("[gdisk] Uploaded -> {}/{}  (id={})".format(subfolder_name, drive_name, file_id))

        return "https://drive.google.com/file/d/{}/view".format(file_id)

    except Exception as exc:  # noqa: BLE001
        print("[gdisk] WARNING: Drive upload failed for {}: {}".format(drive_name, exc))
        return None
