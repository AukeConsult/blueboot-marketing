# gdisk Interface — Google Drive folder integration

`GdiskInterface` is a small, reusable wrapper that lets any function read and write
documents in a single configured Google Drive folder (the "gdisk catalogue"), using the
same Google service account the CRM functions already use for Sheets.

- Code: `functions-crm/crm/gdisk_interface.py`
- HTTP API: `functions-crm/main.py` (routes under `/api/crm/gdisk/...`)
- UI: `public/gdisk.html` (linked from the home page as **Drive Folder**)

---

## 1. The settings

There is exactly **one** thing to configure: the **Drive folder ID** the interface
operates inside. It is resolved in this order — the first non-empty value wins:

| Priority | Source | Where to set it |
|---|---|---|
| 1 | Constructor argument | `GdiskInterface(folder_id="…")` in code |
| 2 | Firestore settings doc | `settings/gdisk` → field `folder_id` |
| 3 | Environment variable | `GDISK_FOLDER_ID` |

For normal operation use **option 2** (Firestore) — it can be changed at runtime from the
UI without redeploying. The env var (option 3) is a convenient fallback for local runs.

### The Firestore setting (recommended)

```
Collection: settings
Document:   gdisk
{
  "folder_id":  "1AbCdEfGhIjKlMnOpQrStUvWxYz",   // the only required field
  "updated_at": "2026-06-05T12:00:00+00:00"      // written automatically on save
}
```

You can set this three ways:

- **From the UI** — open the **Drive Folder** page, paste the folder ID, click *Save folder*.
- **Via the API** — `POST /api/crm/gdisk/settings` with body `{"folder_id": "…"}`.
- **Directly in Firestore** — create/edit `settings/gdisk`.

### The environment-variable setting (fallback)

```
GDISK_FOLDER_ID = 1AbCdEfGhIjKlMnOpQrStUvWxYz
```

For Cloud Functions, set it as a function environment variable (e.g. in `firebase`
functions config / `.env` for the functions runtime). If both the Firestore doc and the
env var are present, the Firestore value wins.

### Where to find the folder ID

It is the last path segment of the Drive folder URL:

```
https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz
                                        └──────────── folder_id ────────────┘
```

---

## 2. One-time Google setup (required)

These are platform settings, not code — without them every call fails with a permission
or "API not enabled" error.

1. **Enable the Google Drive API** on the `blueboot-market` GCP project
   (APIs & Services → Enable APIs → "Google Drive API"). The Sheets API is already on;
   Drive is a separate API.

2. **Share the Drive folder with the service account.** Open the folder in Drive →
   *Share* → add the service-account email as **Editor**:

   ```
   blueboot-market@appspot.gserviceaccount.com
   ```

   (This is the Application Default identity the functions run as. Read-only is enough
   for listing/downloading; Editor is required for upload/delete.)

3. **OAuth scope** — the interface requests `https://www.googleapis.com/auth/drive`.
   No action needed for a service account beyond the two steps above; the scope is set
   in code (`GdiskInterface.SCOPES`).

---

## 3. Settings reference (constants)

Defined in `functions-crm/crm/gdisk_interface.py`:

| Constant | Default | Meaning |
|---|---|---|
| `SETTINGS_COLLECTION` | `"settings"` | Firestore collection holding the setting |
| `SETTINGS_DOC` | `"gdisk"` | Firestore document id for the setting |
| `GDISK_FOLDER_ID_ENV` | `"GDISK_FOLDER_ID"` | Env-var name for the fallback folder id |
| `GdiskInterface.SCOPES` | `["…/auth/drive"]` | OAuth scope requested |

---

## 4. Using the class

```python
from crm.gdisk_interface import GdiskInterface

# Resolve the folder from settings/gdisk (or the env var):
gd = GdiskInterface.from_settings(db)        # db = Firestore client

if not gd.is_configured():
    raise RuntimeError("No gdisk folder configured")

# Write / read JSON
gd.write_json("filter_facets_site_leads.json", facets)
facets = gd.read_json("filter_facets_site_leads.json", default={})

# Write / read text or raw bytes
gd.write_text("notes.txt", "hello")
gd.write_bytes("logo.png", png_bytes, mime="image/png")
raw = gd.read_bytes("logo.png")

# List / inspect / delete
for f in gd.list_files():
    print(f["name"], f.get("size"), f.get("modifiedTime"))
meta = gd.get_meta("logo.png")               # {id, name, mimeType, size, modifiedTime}
gd.delete_file("notes.txt")
```

Notes:
- `write_*` is **create-or-overwrite by name** within the configured folder (it finds an
  existing file with that name and updates it, otherwise creates a new one).
- `read_*` returns `None` (or your `default` for `read_json`) when the file is absent.
- You can pass an explicit folder per call: `GdiskInterface(folder_id="…")`.

---

## 5. HTTP API (crmApi)

Base URL: `https://us-central1-blueboot-market.cloudfunctions.net/crmApi`

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/crm/gdisk/settings` | Get current `folder_id` + `configured` flag |
| POST | `/api/crm/gdisk/settings` | Set `folder_id` (body `{"folder_id": "…"}`) |
| GET | `/api/crm/gdisk/files` | List files in the folder |
| POST | `/api/crm/gdisk/files` | Upload (multipart form field `file`) |
| GET | `/api/crm/gdisk/files/<name>` | Download the file (attachment) |
| DELETE | `/api/crm/gdisk/files/<name>` | Delete the file |

All endpoints return `{"status":"error","message":"…"}` with a 4xx/5xx code on failure;
the list/upload/download endpoints return `400` with a clear message when no folder is
configured.

---

## 6. UI

`public/gdisk.html` (home page → **Drive Folder**) provides:
- a folder-ID setting field with **Save folder**,
- drag-&-drop / multi-file **upload**,
- a file table with **Download** and **Delete** per row, and a **refresh** button.

---

## 7. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `No gdisk folder configured` | `settings/gdisk.folder_id` and `GDISK_FOLDER_ID` are both empty — set one. |
| `403` / `insufficientPermissions` | Folder not shared with the service account, or Drive API not enabled. |
| `404` on download/delete | File name doesn't exist in the configured folder (names are matched exactly, case-sensitive). |
| Uploaded file lands in the wrong place | Wrong `folder_id` — confirm it's the folder, not a file, ID. |
| Changes not taking effect | Redeploy functions after code changes: `firebase deploy --only functions:crm`. |
