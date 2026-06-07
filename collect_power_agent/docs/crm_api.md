# CRM API reference

All endpoints are served by the `crmApi` Cloud Function. Base URL:

```
https://us-central1-<YOUR_PROJECT_ID>.cloudfunctions.net/crmApi
```

CORS is open (the static pages in `public/` call these directly). Responses are JSON
unless noted. Errors return `{"status":"error","message":"…"}` with a 4xx/5xx code.

Long-running work runs as **jobs**: a trigger endpoint returns a `job_id` immediately,
Cloud Tasks invokes `crmWorker`, and you poll `GET /api/crm/status/<job_id>` for the
result. See [Jobs](#jobs).

---

## Diagnostics

| Method | Path | Description |
|---|---|---|
| GET | `/` | Service index / health check. |
| GET | `/api/crm/whoami` | Debug: returns the identity/config the function runs as. |

---

## Pipeline jobs (trigger → poll)

Each of these creates a job and returns `{status:"queued", job_id, poll}`.

| Method | Path | Query params | Description |
|---|---|---|---|
| GET | `/api/crm/contact-sync` | `countries` (csv, default `NO`), `max`, `status`, `campaign`, `min_pages`, `max_pages` | Import contacts from `email_contacts` into the contact sheet. |
| GET | `/api/crm/push-and-sync` | — | Push selected sheet rows to the CRM template. |
| GET | `/api/crm/template-sync` | — | Sync the CRM template back to the Leads Database. |
| GET | `/api/crm/campaign-sync` | `campaign_id` (required), `force` | Sync one campaign from the contact sheet to Firestore. |
| GET | `/api/crm/discover-campaigns` | — | Scan the contact sheet for campaign IDs; create/sync new ones. |

Example:

```
GET /api/crm/contact-sync?countries=NO,SE&max=500&min_pages=500
-> { "status":"queued", "job_id":"6a42ed55", "poll":"/api/crm/status/6a42ed55" }
```

---

## Jobs

| Method | Path | Query params | Description |
|---|---|---|---|
| GET | `/api/crm/status/<job_id>` | — | Get one job: `status` (`queued`/`running`/`done`/`error`), `result`, `error`, timings. |
| GET | `/api/crm/jobs` | `limit` (≤100, default 20), `running` (bool), `campaign_id`, `since` (minutes) | List recent jobs, newest first. |
| POST | `/api/crm/worker/<name>/<job_id>` | — | **Internal** — invoked by Cloud Tasks only. Do not call directly. |

---

## Campaigns

| Method | Path | Body | Description |
|---|---|---|---|
| GET | `/api/crm/campaigns` | — | List campaigns (`?status=` to filter), newest first. |
| GET | `/api/crm/campaigns/<id>` | — | Get one campaign incl. its `campaign_contacts`. |
| POST | `/api/crm/campaigns/<id>/create` | `{outreach_email_account?}` | Create a campaign (409 if it exists). |
| POST·PATCH | `/api/crm/campaigns/<id>` | campaign fields (status, mail, …) | Update a campaign. |
| DELETE | `/api/crm/campaigns/<id>` | — | Delete a **draft** campaign. Atomically flips status to `deleting` in a Firestore transaction, then enqueues a `campaign-delete` job that batch-deletes all `campaign_contacts` and the campaign doc. Returns `{job_id, poll}`. Returns 409 if status is not `draft`. |

---

## Filter facets

The selectable-value catalog used by the Filter Facets page. See also
[`docs/gdisk_interface.md`](gdisk_interface.md) and the facet builder
`app/build_filter_facets.py`.

| Method | Path | Body | Description |
|---|---|---|---|
| GET | `/api/crm/filter-facets` | — | List facet docs (catalog + saved presets). |
| GET | `/api/crm/filter-facets/<name>` | — | Get one facet doc (e.g. `site_leads`). |
| POST·PATCH | `/api/crm/filter-facets/<name>` | full facets object (must contain `filters`) | Save a preset; also **enqueues a `filter-count` job** that refreshes keywords, counts matching sites/contacts and stores `counts` (including `selected_count` per value) back. Returns `{job_id, poll}`. |
| POST | `/api/crm/filter-facets/<name>/create-campaign` | `{campaign_id, dry_run?}` | Create (or refresh) a campaign from a saved facet preset. Streams `email_contacts`, applies the saved filter selections, deduplicates against existing campaign contacts, and writes matching contacts to `campaigns/<campaign_id>/campaign_contacts`. Stores `source_facet_path`, `source_facet_filters` (selection snapshot), and `source_facet_built_at` on the campaign doc. Enqueues a `facet-campaign` job; returns `{job_id, poll}`. Button is only enabled on the UI when `contacts_in_email_contacts > 0`. |

---

## gdisk (Google Drive folder)

File operations on the configured Drive folder. All Drive access is server-side via
`GdiskInterface`; full setup in [`docs/gdisk_interface.md`](gdisk_interface.md).

| Method | Path | Body / params | Description |
|---|---|---|---|
| GET | `/api/crm/gdisk/settings` | — | `{folder_id, configured}`. |
| POST·PATCH | `/api/crm/gdisk/settings` | `{folder_id}` | Set the Drive folder id (stored in `settings/gdisk`). |
| GET | `/api/crm/gdisk/files` | — | List files: `{folder_id, files:[{id,name,size,mimeType,modifiedTime}]}`. |
| POST | `/api/crm/gdisk/files` | multipart form field `file` | Upload (create-or-overwrite by name). |
| GET | `/api/crm/gdisk/files/<name>` | — | Download the file as an attachment (raw bytes). |
| DELETE | `/api/crm/gdisk/files/<name>` | — | Delete the file. |

Returns `400` if no folder is configured. Names are matched exactly, case-sensitive.

---

## Notes

- **Deploy:** `firebase deploy --only functions:crm` (deploys both `crmApi` and `crmWorker`).
- **Auth:** the functions run as the project service account
  (`<YOUR_PROJECT_ID>@appspot.gserviceaccount.com`); Sheets/Drive resources must be shared
  with it, and the relevant Google APIs enabled.
- **Trigger endpoints are GET** for easy use from links/the dashboard; they only enqueue
  work, the actual run happens in `crmWorker`.
