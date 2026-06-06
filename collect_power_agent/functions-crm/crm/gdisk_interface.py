"""gdisk_interface.py -- General Google Drive ("gdisk") read/write interface.

A small, reusable wrapper any function can use to read/write documents in a
configured Google Drive folder (the "gdisk catalogue"), using the same service
account the CRM functions already use for Sheets.

Configuration (the general setting) is resolved in this order:
  1. an explicit folder_id passed to the constructor
  2. a Firestore settings doc  -> settings/gdisk  { "folder_id": "<drive-folder-id>" }
  3. the GDISK_FOLDER_ID environment variable

The service account email (e.g. <project>@appspot.gserviceaccount.com) must be
granted access to the Drive folder (share the folder with it), and the Google
Drive API must be enabled on the project.

Usage:
    from crm.gdisk_interface import GdiskInterface

    gd = GdiskInterface.from_settings(db)          # folder from settings/gdisk or env
    gd.write_json("filter_facets_site_leads.json", facets)
    data = gd.read_json("filter_facets_site_leads.json", default={})
    for f in gd.list_files():
        print(f["name"], f["modifiedTime"])
"""
from __future__ import annotations

import io
import json
import os

SETTINGS_COLLECTION = "settings"
SETTINGS_DOC = "gdisk"
GDISK_FOLDER_ID_ENV = "GDISK_FOLDER_ID"

# Built once per warm instance and reused -- avoids re-auth + discovery on
# every request (the main cause of slow uploads).
_DRIVE_SERVICE = None


class GdiskInterface:
    """Read/write documents in a Google Drive folder. Reusable across functions."""

    SCOPES = ["https://www.googleapis.com/auth/drive"]

    def __init__(self, folder_id: str = "", service=None) -> None:
        self.folder_id = folder_id or os.environ.get(GDISK_FOLDER_ID_ENV, "")
        self._service = service

    # -- construction ------------------------------------------------------
    @classmethod
    def from_settings(cls, db=None, service=None) -> "GdiskInterface":
        """Build using the general setting: Firestore settings/gdisk.folder_id,
        falling back to the GDISK_FOLDER_ID env var."""
        folder_id = os.environ.get(GDISK_FOLDER_ID_ENV, "")
        if db is not None:
            try:
                snap = db.collection(SETTINGS_COLLECTION).document(SETTINGS_DOC).get()
                if snap.exists:
                    folder_id = (snap.to_dict() or {}).get("folder_id") or folder_id
            except Exception:
                pass
        return cls(folder_id=folder_id, service=service)

    @property
    def service(self):
        global _DRIVE_SERVICE
        if self._service is not None:
            return self._service
        if _DRIVE_SERVICE is None:
            import google.auth
            from googleapiclient.discovery import build
            creds, _ = google.auth.default(scopes=self.SCOPES)
            _DRIVE_SERVICE = build("drive", "v3", credentials=creds,
                                   cache_discovery=False)
        return _DRIVE_SERVICE

    def is_configured(self) -> bool:
        return bool(self.folder_id)

    # -- lookups -----------------------------------------------------------
    def find_file(self, name: str, folder_id: str = "") -> str | None:
        """Return the file id for `name` in the folder, or None."""
        fid = folder_id or self.folder_id
        q = f"name = '{name}' and trashed = false"
        if fid:
            q += f" and '{fid}' in parents"
        res = self.service.files().list(
            q=q, spaces="drive", pageSize=1,
            fields="files(id, name)",
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        files = res.get("files", [])
        return files[0]["id"] if files else None

    def list_files(self, folder_id: str = "") -> list[dict]:
        fid = folder_id or self.folder_id
        q = "trashed = false"
        if fid:
            q += f" and '{fid}' in parents"
        res = self.service.files().list(
            q=q, pageSize=1000,
            fields="files(id, name, modifiedTime, mimeType, size)",
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        return res.get("files", [])

    def get_meta(self, name: str) -> dict | None:
        """Return {id, name, mimeType, size, modifiedTime} for a file, or None."""
        fid = self.find_file(name)
        if not fid:
            return None
        return self.service.files().get(
            fileId=fid, fields="id, name, mimeType, size, modifiedTime",
            supportsAllDrives=True).execute()

    def service_account_email(self) -> str:
        """Best-effort: the email of the service account the backend runs as."""
        env = os.environ.get("GDISK_SERVICE_ACCOUNT", "").strip()
        if env:
            return env
        try:
            import google.auth
            creds, _ = google.auth.default(scopes=self.SCOPES)
            email = getattr(creds, "service_account_email", None)
            if email and email != "default":
                return email
        except Exception:
            pass
        try:
            import urllib.request
            req = urllib.request.Request(
                "http://metadata.google.internal/computeMetadata/v1/instance/"
                "service-accounts/default/email",
                headers={"Metadata-Flavor": "Google"})
            return urllib.request.urlopen(req, timeout=2).read().decode().strip()
        except Exception:
            return "(unknown — check the function's runtime service account in GCP)"

    def check_access(self) -> dict:
        """Report what the service account can do with the configured folder.
        Never raises -- returns a dict describing the result."""
        if not self.is_configured():
            return {"configured": False,
                    "reason": "No folder configured (settings/gdisk or GDISK_FOLDER_ID)."}
        try:
            meta = self.service.files().get(
                fileId=self.folder_id,
                fields="id, name, mimeType, capabilities(canListChildren, canAddChildren, canEdit)",
                supportsAllDrives=True).execute()
        except Exception as exc:
            return {"configured": True, "folder_id": self.folder_id,
                    "ok": False, "error": str(exc)}
        caps = meta.get("capabilities", {}) or {}
        is_folder = meta.get("mimeType") == "application/vnd.google-apps.folder"
        return {
            "configured":   True,
            "ok":           True,
            "folder_id":    self.folder_id,
            "name":         meta.get("name"),
            "is_folder":    is_folder,
            "can_read":     bool(caps.get("canListChildren")),
            "can_write":    bool(caps.get("canAddChildren") or caps.get("canEdit")),
            "service_account": self.service_account_email(),
        }

    # -- read --------------------------------------------------------------
    def read_bytes(self, name: str) -> bytes | None:
        fid = self.find_file(name)
        if not fid:
            return None
        from googleapiclient.http import MediaIoBaseDownload
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(
            buf, self.service.files().get_media(fileId=fid, supportsAllDrives=True))
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue()

    def read_text(self, name: str) -> str | None:
        raw = self.read_bytes(name)
        return None if raw is None else raw.decode("utf-8")

    def read_json(self, name: str, default=None):
        txt = self.read_text(name)
        if txt is None:
            return default
        return json.loads(txt)

    # -- write -------------------------------------------------------------
    def write_bytes(self, name: str, data: bytes,
                    mime: str = "application/octet-stream") -> str:
        """Create the file, or overwrite it if it already exists. Returns file id."""
        from googleapiclient.http import MediaInMemoryUpload
        media = MediaInMemoryUpload(data, mimetype=mime, resumable=False)
        fid = self.find_file(name)
        if fid:
            self.service.files().update(
                fileId=fid, media_body=media, supportsAllDrives=True).execute()
            return fid
        meta: dict = {"name": name}
        if self.folder_id:
            meta["parents"] = [self.folder_id]
        created = self.service.files().create(
            body=meta, media_body=media, fields="id", supportsAllDrives=True).execute()
        return created["id"]

    def write_text(self, name: str, content: str, mime: str = "text/plain") -> str:
        return self.write_bytes(name, content.encode("utf-8"), mime=mime)

    def write_json(self, name: str, data) -> str:
        return self.write_text(
            name, json.dumps(data, indent=2, ensure_ascii=False),
            mime="application/json")

    def ensure_sheet(self, name: str) -> str:
        """Return the id of a Google Sheet named <name> in the folder, creating
        it if absent. Used so a campaign always maps to one sheet of the same name."""
        fid = self.find_file(name)
        if fid:
            return fid
        meta: dict = {"name": name,
                      "mimeType": "application/vnd.google-apps.spreadsheet"}
        if self.folder_id:
            meta["parents"] = [self.folder_id]
        created = self.service.files().create(
            body=meta, fields="id", supportsAllDrives=True).execute()
        return created["id"]

    def delete_file(self, name: str) -> bool:
        """Move the file to trash (recoverable). Trashing works for editors,
        whereas a hard delete requires ownership. Returns False if not found."""
        fid = self.find_file(name)
        if not fid:
            return False
        try:
            self.service.files().update(
                fileId=fid, body={"trashed": True}, supportsAllDrives=True).execute()
        except Exception:
            # fall back to a hard delete (works when the SA owns the file)
            self.service.files().delete(fileId=fid, supportsAllDrives=True).execute()
        return True
