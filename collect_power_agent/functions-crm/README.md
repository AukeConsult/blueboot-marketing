# functions-crm — CRM Cloud Functions

Three Firebase Cloud Functions served from a single Flask app:

- **`crmApi`** — short-lived trigger/query endpoints (30 s timeout). Returns immediately with a `job_id` for anything long-running.
- **`smartMail`** — short-lived Smart Mail trigger endpoints only: outreach send, inbound read, and reply match.
- **`crmWorker`** — long-running worker (15 min timeout, 1 GB RAM). Called by Cloud Tasks; does the actual work and writes the result back to `crm_jobs/{job_id}`.

Poll `GET /api/crm/status/{job_id}` for job completion.

---

## Project structure

```
functions-crm/
├── main.py               ← thin shell: registers blueprints + exposes Cloud Functions
├── requirements.txt
│
├── handlers/             ← one Blueprint per domain (all business logic lives here)
│   ├── shared.py         ← shared infrastructure (DB, jobs, Cloud Tasks, helpers)
│   ├── imap_utils.py     ← shared IMAP helpers (no routes)
│   ├── campaigns.py      ← campaign CRUD + discover
│   ├── contacts.py       ← contact GET/PATCH + followup-contacts collection-group
│   ├── jobs.py           ← all job triggers + worker dispatcher + status/list
│   ├── mailbox.py        ← IMAP mailbox reading (read_mailbox, read_message_body)
│   ├── mail_tags.py      ← mailbox tag CRUD + IMAP keyword sync
│   ├── mail_accounts.py  ← mail account settings (CRUD, ping, test-send)
│   ├── inbound_read.py ← inbound mail read job trigger
│   ├── gdisk.py          ← Google Drive folder endpoints
│   ├── filter_facets.py  ← filter facets + facet-to-campaign
│   ├── leads.py          ← lead lookup, exclusion, name-enrich
│   ├── statistics.py     ← statistics collect + get
│   └── auth.py           ← user doc management
│
└── crm/                  ← pure-Python job libraries (no Flask, no routes)
    ├── campaign_export_lib.py
    ├── campaign_sync_lib.py
    ├── campaign_delete_lib.py
    ├── contact_sync_lib.py
    ├── crm_sync_lib.py
    ├── crm_template_sync_lib.py
    ├── facet_campaign_lib.py
    ├── filter_count_lib.py
    ├── inbound_read_lib.py
    ├── gdisk_interface.py
    ├── mail_sender.py
    ├── name_enrich_lib.py
    ├── push_and_sync_lib.py
    ├── sheets_config.py
    └── statistics_builder.py
```

---

## Architecture

```
main.py
  └── registers 12 Blueprints from handlers/
        │
        ├── Each Blueprint owns one domain (campaigns, mailbox, etc.)
        ├── All Blueprints import shared infra from handlers/shared.py
        └── Long-running work is delegated to crm/ lib files
                (called by jobs.py worker dispatcher)
```

**Key rule:** nothing in `handlers/` imports from another handler. All cross-handler
dependencies go through `handlers/shared.py` or `handlers/imap_utils.py`.

---

## handlers/shared.py

Single source of truth for shared infrastructure. Every handler imports from here:

| Symbol | Purpose |
|---|---|
| `_get_db()` | Firestore client (thread-safe singleton) |
| `_sheets_service()` | Google Sheets API service |
| `_gdisk()` | GdiskInterface from Firestore settings |
| `_ma_col(db)` | Shortcut to `settings/mail_accounts/accounts` |
| `_get_mail_account(db, email)` | Fetch a mail account config doc |
| `_new_job(name, params)` | Create a job doc in `crm_jobs`, return `job_id` |
| `_update_job(job_id, **kwargs)` | Update a job doc |
| `_enqueue_task(name, job_id, params)` | Enqueue a Cloud Tasks HTTP call to crmWorker |
| `_ok(message, **kwargs)` | `{"status": "ok", ...}` response |
| `_err(message, code)` | `{"status": "error", ...}` response |
| `_accepted(job_id, name)` | 202 queued response with poll URL |

---

## handlers/jobs.py — the worker dispatcher

The `POST /api/crm/worker/<name>/<job_id>` route handles all job types. Each `elif`
branch lazy-imports the relevant `crm/` lib and calls its `run_*` function.

**To add a new job type:**
1. Add the lib function to `crm/<job_name>_lib.py`
2. Add a trigger endpoint in the appropriate handler file (or `inbound_read.py` as a template)
3. Add an `elif name == "<job-name>":` branch in `handlers/jobs.py`
4. Add a CLI script `app/<job_name>.py` and launchers `run_<job_name>.bat` / `.sh`
5. Document in `README.md` and the relevant user guide

---

## Registered routes (59 total)

| Handler | Count | URL prefix |
|---|---|---|
| `campaigns.py` | 8 | `/api/crm/campaigns`, `/api/crm/discover-campaigns` |
| `contacts.py` | 4 | `/api/crm/campaigns/<id>/contacts`, `/api/crm/followup-contacts` |
| `jobs.py` | 12 | `/api/crm/contact-sync`, `/api/crm/campaign-*`, `/api/crm/worker`, `/api/crm/status`, `/api/crm/jobs` |
| `mailbox.py` | 2 | `/api/crm/settings/mail-accounts/<email>/mailbox` and `/message` |
| `mail_tags.py` | 5 | `/api/crm/mailbox-tags`, `/api/crm/settings/mail-tag-statuses` |
| `mail_accounts.py` | 5 | `/api/crm/settings/mail-accounts` |
| `inbound_read.py` | 1 | `/api/crm/inbound-read` |
| `gdisk.py` | 7 | `/api/crm/gdisk` |
| `filter_facets.py` | 4 | `/api/crm/filter-facets` |
| `leads.py` | 4 | `/api/crm/leads`, `/api/crm/name-enrich` |
| `statistics.py` | 2 | `/api/crm/statistics` |
| `auth.py` | 3 | `/api/crm/auth/users` |
| `main.py` | 2 | `/`, `/api/crm/whoami` |

---

## Deploy

```bat
firebase deploy --only functions:crm
```

This deploys the CRM codebase entrypoints exported from `main.py`: `crmApi`,
`smartMail`, and `crmWorker`.

`smartMail` only serves these trigger paths:

```text
POST /outreach-send
POST /inbound-read
POST /reply-match
```

These direct `smartMail` trigger paths are service-authenticated.

The existing CRM API paths are also accepted for compatibility:

```text
POST /api/crm/outreach-send
POST /api/crm/inbound_read
POST /api/crm/reply_match
POST /api/crm/inbound-read
POST /api/crm/reply-match
```

The `/api/crm/...` compatibility trigger paths require `campaign-user` or `admin`.

Scheduled Smart Mail triggers should use `POST`; do not use `GET` for `outreach-send`.

Or the full deploy (functions + hosting):

```bat
deploy_crm.bat
```

