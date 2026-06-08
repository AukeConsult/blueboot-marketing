"""handlers/gdisk.py — Google Drive folder endpoints."""
from __future__ import annotations
from datetime import datetime, timezone
from flask import Blueprint, request
from handlers.shared import _get_db, _gdisk, _ok, _err

GDISK_SETTINGS_COLLECTION = "settings"
GDISK_SETTINGS_DOC        = "gdisk"

bp = Blueprint("gdisk", __name__)


@bp.route("/api/crm/gdisk/settings", methods=["GET"])
def gdisk_get_settings():
    try:
        gd = _gdisk()
        return _ok("ok", folder_id=gd.folder_id, configured=gd.is_configured())
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/gdisk/settings", methods=["POST", "PATCH"])
def gdisk_set_settings():
    try:
        body      = request.get_json(silent=True) or {}
        folder_id = (body.get("folder_id") or "").strip()
        _get_db().collection(GDISK_SETTINGS_COLLECTION).document(GDISK_SETTINGS_DOC).set(
            {"folder_id": folder_id, "updated_at": datetime.now(timezone.utc).isoformat()}, merge=True)
        return _ok("gdisk folder saved", folder_id=folder_id)
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/gdisk/check", methods=["GET"])
def gdisk_check_access():
    try:
        return __builtins__["__import__"]("flask").jsonify(_gdisk().check_access())
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/gdisk/files", methods=["GET"])
def gdisk_list_files():
    try:
        gd = _gdisk()
        if not gd.is_configured():
            return _err("No gdisk folder configured. Set one in settings.", 400)
        from flask import jsonify
        return jsonify({"folder_id": gd.folder_id, "files": gd.list_files()})
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/gdisk/files", methods=["POST"])
def gdisk_upload_file():
    try:
        gd = _gdisk()
        if not gd.is_configured():
            return _err("No gdisk folder configured. Set one in settings.", 400)
        f = request.files.get("file")
        if f is None:
            return _err("No file uploaded (form field 'file').")
        name    = f.filename or "upload.bin"
        data    = f.read()
        mime    = f.mimetype or "application/octet-stream"
        file_id = gd.write_bytes(name, data, mime=mime)
        return _ok(f"Uploaded {name}", name=name, file_id=file_id, bytes=len(data))
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/gdisk/files/<path:name>", methods=["GET"])
def gdisk_download_file(name):
    try:
        from flask import Response
        gd   = _gdisk()
        if not gd.is_configured():
            return _err("No gdisk folder configured.", 400)
        data = gd.read_bytes(name)
        if data is None:
            return _err(f"'{name}' not found in gdisk folder", 404)
        meta = gd.get_meta(name) or {}
        mime = meta.get("mimeType") or "application/octet-stream"
        return Response(data, mimetype=mime,
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/gdisk/files/<path:name>", methods=["DELETE"])
def gdisk_delete_file(name):
    try:
        gd = _gdisk()
        if not gd.is_configured():
            return _err("No gdisk folder configured.", 400)
        ok = gd.delete_file(name)
        if not ok:
            return _err(f"'{name}' not found", 404)
        return _ok(f"Deleted {name}", name=name)
    except Exception as exc:
        return _err(str(exc), 500)
