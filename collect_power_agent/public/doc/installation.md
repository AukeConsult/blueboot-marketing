# Installation Guide

This guide covers setting up the full Blueboot CRM system from scratch: Google Cloud project, Firebase, the Python environment, API keys, deployment and the first admin user.

The system has four deployable parts. Know which one a change belongs to, because each has its own deploy command:

| Part | Folder | What it is | Deploy |
|---|---|---|---|
| **CRM API** | `functions-crm/` | Python 3.12 Firebase codebase `crm`: the functions `crmApi` (REST API), `crmWorker` (background jobs) and `smartMail` (scheduler entry for sending and reading mail) | `firebase deploy --only functions:crm` |
| **Auth triggers** | `functions-auth/` | Node 22 Firebase codebase `auth`: mirrors every Firebase Auth user into Firestore | `firebase deploy --only functions:auth` |
| **Dashboard** | `public/` | The web pages, on Firebase Hosting | `firebase deploy --only hosting` |
| **Cloud Batch** (optional) | `cloud_batch/` + `app/` | Pipeline runner on Cloud Run with Cloud Scheduler crons | `bash deploy_batch.sh` |

The local pipeline scripts in `app/` run on your own computer and need only the Python environment (section 3).

---

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | 3.11 or 3.12 | 3.12 recommended (matches the Cloud Function runtime) |
| Node.js | 18+ | Needed for the Firebase CLI and `npm install` in `functions-auth/` (the deployed runtime is Node 22) |
| Firebase CLI | Latest | `npm install -g firebase-tools` |
| Google Cloud CLI | Latest | `gcloud`, for the GCP setup script and Cloud Batch |
| Git | Any | |

---

## 1. Google Cloud & Firebase project

### 1.1 Create the Firebase project

1. Go to [console.firebase.google.com](https://console.firebase.google.com)
2. Click **Add project** and give it a name (e.g. `my-crm-project`)
3. Enable **Google Analytics** if desired, then create
4. In the project dashboard, go to **Build → Firestore Database** and create it in **production mode**. Choose a region (e.g. `us-central1`)
5. Go to **Build → Hosting → Get started** (follow the wizard; you deploy later)

Point the project at your Firebase project ID in `.firebaserc`:

```json
{ "projects": { "default": "YOUR_PROJECT_ID" } }
```

or run `firebase use YOUR_PROJECT_ID`.

> The scripts and the dashboard in this repository are set up for the project `blueboot-market`. When you install into a **different** project, replace that ID in `setup_gcp.sh` / `setup_gcp.bat`, the `deploy_*` scripts, the `deploy:batch` line in `package.json`, and the `BASE` constant in `public/js/crm-common.js` (section 5.4).

### 1.2 Enable the required GCP APIs

Run the included setup script (Linux/Mac):
```bash
./setup_gcp.sh
```

Or on Windows:
```bat
setup_gcp.bat
```

The script does the following:

- enables the **Cloud Tasks, Cloud Functions, Cloud Build, Cloud Scheduler and Artifact Registry** APIs,
- creates the Cloud Tasks queue `crm-queue` (used for background jobs),
- grants `roles/cloudtasks.enqueuer` and `roles/run.invoker` to the App Engine default service account,
- grants `roles/cloudscheduler.admin` and `roles/run.invoker` to the Compute default service account (the batch runner uses these to manage and be called by Cloud Scheduler).

To run the main steps manually:
```bash
gcloud config set project YOUR_PROJECT_ID
gcloud services enable cloudtasks.googleapis.com cloudfunctions.googleapis.com \
    cloudbuild.googleapis.com cloudscheduler.googleapis.com artifactregistry.googleapis.com
gcloud tasks queues create crm-queue --location=us-central1
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
    --member="serviceAccount:YOUR_PROJECT_ID@appspot.gserviceaccount.com" \
    --role="roles/cloudtasks.enqueuer"
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
    --member="serviceAccount:YOUR_PROJECT_ID@appspot.gserviceaccount.com" \
    --role="roles/run.invoker"
```

### 1.3 Enable Firebase Authentication

1. In the Firebase console go to **Build → Authentication → Get started**
2. Under **Sign-in method** enable **Google** and **Email/Password**
3. Under **Settings → Authorised domains**, confirm your hosting domain is listed
   (Firebase adds `*.firebaseapp.com` and `*.web.app` automatically)

### 1.4 Create the web app and save the config

1. In the Firebase console go to Project settings (gear icon) → **Your apps** → **Add app** → Web (`</>`)
2. Register the app (nickname e.g. "CRM frontend") and copy the `firebaseConfig` object shown
3. In the project, copy the template:
   ```bash
   cp public/firebase-config.example.js public/firebase-config.js
   ```
4. Open `public/firebase-config.js` and paste your values into `window.FIREBASE_CONFIG`:
   ```js
   window.FIREBASE_CONFIG = {
     apiKey:            "AIzaSy...",
     authDomain:        "your-project.firebaseapp.com",
     projectId:         "your-project",
     storageBucket:     "your-project.appspot.com",
     messagingSenderId: "123456789",
     appId:             "1:123456789:web:abc123",
     measurementId:     "G-XXXXXXX"   // optional
   };
   ```
5. **`firebase-config.js` is gitignored.** Never commit it. The template `firebase-config.example.js` is committed instead.

### 1.5 Download the service account key

1. In the Firebase console go to Project settings → **Service accounts**
2. Click **Generate new private key** and save the JSON file
3. Place it at `config/serviceAccountKey.json`
4. **Never commit this file.** It is listed in `.gitignore`

The deployed functions do not use this file; they use the project's default credentials. The key is for the local scripts in `app/`.

---

## 2. Environment variables

Copy the example file and fill in your values:

```bash
cp .env.example .env
```

The main settings in `.env`:

```ini
# Firebase / Firestore
FIREBASE_CREDENTIALS=config/serviceAccountKey.json   # path to the key file
FIREBASE_KEY_JSON=                                    # or the key inline as JSON (takes precedence)
FIRESTORE_COLLECTION=leads

# OpenAI: required for AI enrichment
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini

# Search: Brave is required for contact enrichment
BRAVE_API_KEY=
GOOGLE_API_KEY=
GOOGLE_CSE_ID=
GITHUB_TOKEN=            # optional, improves GitHub rate limits (no scopes needed)

# Crawler tuning (the defaults are good for most cases)
MAX_RESULTS=200
MIN_SCORE=50
MAX_PAGES=6
MAX_COUNTRY=1000
GIVE_UP_AFTER=15
CRAWL_DELAY=1.0
CRAWL_WORKERS=20
LIMIT_PER_HOST=3
```

`.env.example` also has optional entries for campaign folders and the legacy Gmail sender; leave them empty unless you use them.

**Required keys:**

| Key | Where to get it | Used by |
|---|---|---|
| `OPENAI_API_KEY` | [platform.openai.com](https://platform.openai.com) | All AI enrichment scripts |
| `BRAVE_API_KEY` | [brave.com/search/api](https://brave.com/search/api) | Contact enrichment |
| Firebase service account | Firebase console → Service accounts | All local Firestore access |

**Optional but recommended:**

| Key | Where to get it | Used by |
|---|---|---|
| `GITHUB_TOKEN` | GitHub → Settings → Developer tokens | Agency discovery via GitHub |
| `GOOGLE_API_KEY` + `GOOGLE_CSE_ID` | Google Cloud Console → Custom Search | Supplementary search |

**Variables used when deploying the CRM function.** If you use Cloud Batch (section 6), add these to the **root** `.env`:

```ini
BATCH_RUNNER_URL=https://...        # printed at the end of Cloud Batch setup
BATCH_SECRET=...
BATCH_SA=...
```

`deploy_crm.sh` copies them into `functions-crm/.env` on every deploy. That file is generated, so never edit it by hand. Without Cloud Batch the three values stay empty.

Mail accounts are **not** configured in `.env`. They are added in the dashboard (section 9). The file `.env.smart_mail.example` belongs to an old sender and is not used.

---

## 3. Python environment (local scripts)

The local pipeline scripts (`app/`) use a different environment from the Cloud Function. The function has its own `functions-crm/venv` and its own, smaller `functions-crm/requirements.txt` (section 5).

```bash
# Create the virtual environment in the project root
python -m venv .venv

# Activate (Linux/Mac)
source .venv/bin/activate

# Activate (Windows)
.venv\Scripts\activate

# Install dependencies (a full pinned list)
pip install -r requirements.txt
```

Run every script **from the project root**, for example `python app/site_agent.py`. The scripts import `app/_pathsetup.py`, which adds `app/`, `app/functions/` and `functions-crm/` to the Python path, so the same modules work locally and in the Cloud Function.

Verify the setup with the small checks in `tests/`:

```bash
python tests/test_config.py      # keys present (Firebase, OpenAI, Brave)
python tests/test_firestore.py   # Firestore readable
python tests/test_openai.py      # OpenAI answers
```

---

## 4. Google Sheets setup

Two Google Sheets are used by the **CRM Discover / CRM sync** pages. Those pages are **legacy** and will be removed, so you can skip this section if you do not use them. Campaign sheets are created automatically in the Drive folder (section 8).

### 4.1 Contact sheet (master CRM sheet)
- Create a Google Sheet and name the first tab **contacts**
- Required columns: `doc_id`, `email`, `name`, `title`, `website`, `country`, `campaign`, `status`, `select`
- Copy the sheet ID from the URL (the long string after `/spreadsheets/d/`)
- Set it as `CONTACT_SHEET_ID` (environment variable, or the default in `functions-crm/crm/sheets_config.py`)

### 4.2 CRM work sheet
- Create a second Google Sheet and name the first tab **Outreach**
- The columns are written on the first run of `crm/push_and_sync.py`
- Set its ID as `TEMPLATE_SHEET_ID`

> The defaults in `sheets_config.py` point to the original sheets. Change them to your own IDs.

### 4.3 Share both sheets with the service account

Go to each sheet → Share → paste the service account email and give it **Editor** access:
```
YOUR_PROJECT_ID@appspot.gserviceaccount.com
```

---

## 5. Deploying the Cloud Functions and the dashboard

### 5.1 Log in

```bash
firebase login
firebase use YOUR_PROJECT_ID
```

### 5.2 Install the dependencies for the two function codebases

```bash
# CRM API (Python)
python -m venv functions-crm/venv
functions-crm/venv/bin/pip install -r functions-crm/requirements.txt     # Windows: functions-crm\venv\Scripts\pip.exe

# Auth triggers (Node)
cd functions-auth && npm install && cd ..
```

### 5.3 Deploy

| What changed | Command |
|---|---|
| Dashboard pages (`public/`) | `npm run deploy:hosting` (or `firebase deploy --only hosting`) |
| CRM API (`functions-crm/`) | `npm run deploy:crm` (or `firebase deploy --only functions:crm`) |
| Auth triggers (`functions-auth/`) | `npm run deploy:auth` (or `firebase deploy --only functions:auth`) |
| All Firebase functions | `npm run deploy:functions` |
| Firestore rules and indexes | `firebase deploy --only firestore` |
| Pipeline scripts (`app/`, `cloud_batch/`) | `bash deploy_batch.sh` (section 6) |

**Full deploy in one step.** `deploy_crm.sh` (Windows: `deploy_crm.bat`, also `npm run deploy:crm:full`) does the following:

1. writes `functions-crm/.env` from the root `.env`,
2. creates `functions-crm/venv` if it is missing and installs the requirements,
3. runs `firebase deploy --only functions`,
4. runs `firebase deploy --only hosting`.

`deploy_all.sh` (Windows: `deploy_all.bat`) runs `deploy_crm` and then `deploy_batch`. Use `--skip-crm` or `--skip-batch` to leave one out.

> **Firestore deploys on Linux/Mac.** `firebase.json` runs a predeploy step that merges the live indexes into `firestore.indexes.json` using `.venv\Scripts\python.exe firestore_sync.py --no-discover`. That path is Windows-style. On Linux/Mac change it to `.venv/bin/python firestore_sync.py --no-discover`, or the Firestore deploy stops at that step.

The CRM codebase deploys three functions:

| Function | Purpose | Timeout |
|---|---|---|
| `crmApi` | REST API for the dashboard (`/api/crm/...`) | 300 s |
| `crmWorker` | Runs background jobs queued through Cloud Tasks | 900 s |
| `smartMail` | Entry for the scheduled mail jobs (`/outreach-send`, `/inbound-read`, `/reply-match`), service authenticated | 540 s, 1 instance |

### 5.4 Verify and set the API URL

The API base URL is:
```
https://us-central1-YOUR_PROJECT_ID.cloudfunctions.net/crmApi
```

Every API call needs a signed-in user, so a plain request must answer `401 Sign in required`. That answer shows the function is deployed and running:
```bash
curl https://us-central1-YOUR_PROJECT_ID.cloudfunctions.net/crmApi/api/crm/whoami
```

Edit `public/js/crm-common.js` and set the `BASE` constant to your API URL:

```js
const BASE = 'https://us-central1-YOUR_PROJECT_ID.cloudfunctions.net/crmApi';
```

Then redeploy the hosting:
```bash
npm run deploy:hosting
```

---

## 6. Cloud Batch (optional)

The `cloud_batch/` framework runs the pipeline scripts unattended on Google Cloud. Set this up if you want scheduled runs or want to start pipelines from the **Cloud Batch** page in the dashboard (admin only).

### 6.1 Prerequisites

Install the `gcloud` CLI and log in:

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
```

No local Docker is needed. The image is built on GCP with Cloud Build. `.gcloudignore` keeps the upload small: virtual environments, `.env`, `node_modules`, `public/`, `functions-crm/`, `crm/` and local exports are not sent.

### 6.2 First-time setup

Run once from the project root:

```bash
bash cloud_batch/setup/setup_all.sh
```

It runs the numbered scripts in `cloud_batch/setup/`:

| Script | Does |
|---|---|
| `01_enable_apis.sh` | Enables Cloud Run, Cloud Build, Artifact Registry, Secret Manager, Cloud Scheduler |
| `02_service_account.sh` | Creates the `batch-runner` service account and roles |
| `03_artifact_registry.sh` | Creates the image repository |
| `04_deploy_cloudrun.sh` | Builds the image with Cloud Build and deploys the `batch-runner` Cloud Run service |
| `05_setup_scheduler.sh` | Creates Cloud Scheduler cron jobs from `cloud_batch/job_definitions/` |
| `06_secrets.sh` | Pushes the secrets from `.env` to Secret Manager |

`teardown.sh` removes the Cloud Run service and the scheduler jobs without touching Firestore data. Setup takes about 5–10 minutes the first time.

The pipelines are defined in `cloud_batch/job_definitions/*.json` (`site_pipeline`, `site_enrich_pipeline`, `lead_pipeline`, `lead_enrich_pipeline`, `test_job`). Each file lists its steps and an optional cron `schedule`. After editing a definition with a schedule, run `bash cloud_batch/setup/05_setup_scheduler.sh` again.

### 6.3 Deploying code changes

After any change to `app/` or `cloud_batch/`:

```bash
bash deploy_batch.sh
```

This rebuilds the image (about 3 minutes) and redeploys Cloud Run. It does not touch the CRM functions or the dashboard.

| What changed | Command |
|---|---|
| `app/` or `cloud_batch/` | `bash deploy_batch.sh` |
| `functions-crm/` | `npm run deploy:crm` |
| `public/` | `npm run deploy:hosting` |
| Secrets in `.env` | `bash cloud_batch/setup/06_secrets.sh`, then `bash deploy_batch.sh` |

### 6.4 Verify

The batch runner URL is printed at the end of `setup_all.sh`. Put it in the root `.env` as `BATCH_RUNNER_URL` (with `BATCH_SECRET` and `BATCH_SA`), then redeploy the CRM function. You can also get the URL with:

```bash
gcloud run services describe batch-runner \
  --platform managed --region us-central1 --project YOUR_PROJECT_ID \
  --format "value(status.url)"
```

Open the dashboard → **Daily Admin → Cloud Batch** to confirm the jobs are listed, then try a manual run.

---

## 7. Firestore indexes and rules

```bash
firebase deploy --only firestore
```

This deploys `firestore.rules` and `firestore.indexes.json`. The indexes are required for the collection-group queries and for the job list. A new index takes a few minutes to build; check its progress in the Firebase console under **Firestore → Indexes**. To deploy only the indexes use `firebase deploy --only firestore:indexes`.

---

## 8. Google Drive folder (optional)

The campaign export feature uploads spreadsheets to a Google Drive folder.

1. Create a folder in Google Drive
2. Share it with the service account email (Editor access)
3. Copy the folder ID from the URL
4. Open the dashboard → **Daily Admin → Drive Folder** → paste the folder ID → **Save folder** → **Check access**

---

## 9. Mail accounts and sending

Configure the outreach mail accounts in the dashboard:

1. Open **Settings → Mail accounts → Add account**
2. Choose **IMAP** or **Google / Gmail**
3. For IMAP: fill in host, port, user name, password and the SMTP settings
4. For Gmail: fill in Client ID, Client Secret and Refresh Token (from Google Cloud Console → OAuth 2.0)
5. Click **Test** to verify the connection

Gmail OAuth2 setup:
1. Google Cloud Console → APIs & Services → Credentials
2. Create an OAuth 2.0 Client ID (Desktop app type)
3. Download the JSON and run the OAuth flow to get a refresh token
4. Paste the credentials into the mail account settings

Then:

- Set the send limits (per hour and per day, bounce threshold) on **Settings → Outreach Settings**. The defaults are 50 per hour and 300 per day per account.
- Choose the account on each campaign.
- To send mail automatically, create a Cloud Scheduler job that calls the `smartMail` function. How to start and stop it is described in [Send Outreach Mail](doc-viewer.html?doc=send-outreach). The scheduler job is created in Google Cloud and is not part of this repository.

---

## 10. Access control: the first admin

Every user needs a role. Without one a signed-in user is a **guest** and sees "Your account is pending access". The roles are `user`, `campaign-user` and `admin` (see the Access Control document in the repository, `readme-access.md`).

1. Deploy the `auth` functions (section 5.3). From then on every sign-in is mirrored into Firestore at `settings/users/users/{email}`.
2. Open the dashboard and sign in with the account that should become the first admin. You arrive as a guest.
3. In the Firebase console go to **Firestore → `settings` → `users` → `users` → your e-mail address** and add the field `role` with the value `admin`.
4. Reload the dashboard. From now on assign roles to everyone else on the **Users** page.

If people signed in before the `auth` functions were deployed, mirror them once with:
```bash
python app/sync_auth_users.py --dry-run     # preview
python app/sync_auth_users.py               # write
```
Existing roles are never overwritten.

---

## 11. First run checklist

Once everything is set up, verify the pipeline from the project root:

```bash
# 1. Keys and connections
python tests/test_config.py
python tests/test_firestore.py
python tests/test_openai.py

# 2. A small discovery run without writing to Firestore
python app/site_agent.py --countries NO --max-results 5 --dry-run

# 3. Build the Leads DB filter values ("filter facets") without writing
python app/build_filter_facets.py --no-write

# 4. Open the dashboard
# https://YOUR_PROJECT_ID.web.app
```

In the dashboard check that you can sign in, that **Campaigns** opens, and that **Daily Admin → Outreach status** loads without an error.

---

## 12. Project structure

```
collect_power_agent/
├── app/                    # Local Python pipeline and maintenance scripts (run from the root)
│   ├── functions/          # Shared utilities (config, firebase, crawl helpers)
│   ├── _pathsetup.py       # Adds app/, functions-crm/ ... to the Python path
│   ├── outreach_send.py, inbound_read.py, reply_match.py   # Mail jobs (CLI)
│   ├── campaign_importer.py, campaign_exporter.py, prospects_import.py
│   └── site_agent.py, site_enrich_agent.py, lead_*.py ...  # Discovery and enrichment
├── config/                 # Keys, query files, blocklists
│   ├── serviceAccountKey.json  ← not committed
│   ├── site_agent_queries.json, countries.json, catalogs.json
│   └── blocklist_domains.txt, site_agent_blocklist.txt
├── cloud_batch/            # Pipeline runner on Cloud Run
│   ├── job_definitions/    # One JSON per pipeline (steps + cron)
│   └── setup/              # setup_all.sh and the numbered setup scripts
├── functions-crm/          # Firebase codebase "crm" (Python 3.12)
│   ├── main.py             # crmApi, crmWorker, smartMail + request authentication
│   ├── auth_settings.py    # Access policy by route
│   ├── handlers/           # One Flask blueprint per area (campaigns, contacts, jobs, ...)
│   ├── crm/                # Business logic (import, scrape, contact cleaning, export, ...)
│   ├── smart_mail/         # Outreach selection, sending, reply matching, send status
│   └── requirements.txt
├── functions-auth/         # Firebase codebase "auth" (Node 22): mirrors sign-ins to Firestore
├── crm/                    # Local CRM sheet-sync scripts (legacy)
├── public/                 # Dashboard (Firebase Hosting)
│   ├── *.html              # Pages
│   ├── js/, css/, vendor/  # Scripts, styles, bundled libraries
│   ├── doc/                # The documentation shown in the doc viewer
│   ├── firebase-config.js          ← not committed (copy from the example)
│   └── firebase-config.example.js  ← template
├── tests/                  # Connection checks (config, Firestore, OpenAI, Brave)
├── docs/                   # Reference documents
├── .env, .env.example      # Settings (.env is not committed)
├── .firebaserc             # Firebase project ID
├── firebase.json           # Hosting, Firestore and the two function codebases
├── firestore.indexes.json, firestore.rules, firestore_sync.py
├── setup_gcp.sh / .bat     # One-time GCP setup
├── deploy_crm.* / deploy_hosting.* / deploy_batch.sh / deploy_all.*
└── run_*.bat / run_*.sh    # Launchers for common scripts (outreach, replies, prospects, ...)
```

---

## 13. Project-specific values: every place to change

The project-specific values now live in **three places only**. Everything else is derived from them, so you no longer search the repo for the project ID.

| Where | What to set |
|-------|-------------|
| `.env` (project root) | `GCP_PROJECT=your-project-id` (plus optional `GCP_LOCATION`, default `us-central1`). Read by the local Python scripts, the `cloud_batch/` scripts, `deploy_batch.sh` and the Cloud Batch setup scripts. |
| `public/firebase-config.js` | The Firebase web config from step 1.4 (`projectId`, `authDomain`, `storageBucket`). The dashboard derives the `crmApi` URL from `projectId`. Optional keys: `functionsRegion` (default `us-central1`) and `apiBase` (full URL override). |
| `.firebaserc` | `projects.default`: the project the Firebase CLI deploys to. |

On Cloud Functions the project ID is detected automatically (`GOOGLE_CLOUD_PROJECT`), and the worker URL and dashboard URL are derived from it, so `functions-crm` needs no project setting.

### 13.1 Still set by hand

| File | What |
|------|------|
| `.env` | `FIREBASE_KEY_JSON` (service account key of the new project), `BATCH_SA` (`batch-runner@<project>.iam.gserviceaccount.com`), `UNSUBSCRIBE_BASE_URL`, `BATCH_RUNNER_URL`. `deploy_crm` copies the `BATCH_*` values to `functions-crm/.env`; do not edit that file. |
| `config/google_oauth_client.json` | OAuth client of your project (only for the legacy Google Sheets / Drive scripts). |
| `package.json` | `deploy:batch` script still contains the project ID. |
| `setup_gcp.sh` / `.bat`, `deploy_all.bat`, `cloudbuild.yaml`, `cloud_batch/cloudbuild.yaml` | Project ID in gcloud commands and the Cloud Build `_PROJECT` substitution. |
| `deploy_*.sh` / `.bat` | Only the dashboard URL printed at the end. |

To find anything that is left:

```bash
grep -rIn --exclude-dir=node_modules --exclude-dir=.git --exclude-dir=venv "your-old-project-id" .
```

### 13.2 Other environment-specific values

- **Mail accounts and passwords:** Firestore (`settings`) and Secret Manager, see chapter 9.
- **Cloud Tasks queue** `crm-queue` and region: `TASKS_QUEUE`, `GCP_LOCATION` in `.env.update` (defaults shown there).
- **First admin e-mail:** chapter 10.
- **API keys:** chapter 2.

After changing values, redeploy: `firebase deploy --only functions:crm`, `npm run deploy:hosting`, and `npm run deploy:batch` if you use Cloud Batch.

## Troubleshooting

**`No module named 'functions'`**: run scripts from the project root, as `python app/<script>.py`. The scripts import `_pathsetup.py`, which adds the right paths.

**`firebase_admin.initialize_app` error**: check that `config/serviceAccountKey.json` exists and belongs to the right project (or that `FIREBASE_KEY_JSON` is set).

**The dashboard shows "Failed to fetch" or a red error on a new page**: the browser could not get an answer from the API. Most often the CRM function has not been deployed since the page was added. Run `npm run deploy:crm`, then check the function logs in the Firebase console.

**API returns 401**: you are not signed in, or the session expired. Sign in again.

**API returns 403**: your role is too low for that call, or the service account is missing `roles/run.invoker` / `roles/cloudtasks.enqueuer`. Run `setup_gcp.sh`.

**"Your account is pending access"**: the account has no role. Set one (section 10).

**Firestore deploy fails at the predeploy step**: `firebase.json` calls the Windows Python path. Change it as described in section 5.3.

**A query fails with "requires an index"**: deploy the indexes (`firebase deploy --only firestore:indexes`) and wait until the index is built.

**Sheets not found (404)**: check the sheet IDs in `sheets_config.py` and that the service account has Editor access.

**No mail is sent automatically**: check **Outreach status** for the reason (campaign not ready, contacts not confirmed, budget used up) and that the Cloud Scheduler job is enabled. See [Send Outreach Mail](doc-viewer.html?doc=send-outreach).
