# Installation Guide

This guide covers setting up the full Blueboot CRM system from scratch — Google Cloud project, Firebase, Python environment, API keys, and config files.

---

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | 3.11 or 3.12 | 3.12 recommended (matches Cloud Function runtime) |
| Node.js | 18+ | Required for Firebase CLI |
| Firebase CLI | Latest | `npm install -g firebase-tools` |
| Google Cloud CLI | Latest | `gcloud` — for GCP setup script |
| Git | Any | |

---

## 1. Google Cloud & Firebase project

### 1.1 Create the Firebase project

1. Go to [console.firebase.google.com](https://console.firebase.google.com)
2. Click **Add project** → give it a name (e.g. `my-crm-project`)
3. Enable **Google Analytics** if desired, then create
4. In the project dashboard, go to **Build → Firestore Database** → create in **production mode**, choose a region (e.g. `us-central1`)
5. Go to **Build → Hosting** → Get started (follow the wizard; you'll deploy later)

### 1.2 Enable required GCP APIs

Run the included setup script (Linux/Mac):
```bash
./setup_gcp.sh
```

Or on Windows:
```bat
setup_gcp.bat
```

This enables Cloud Tasks, Cloud Functions, and Cloud Build APIs, creates the `crm-queue` Cloud Tasks queue, and grants the required IAM roles to the App Engine default service account.

To run manually:
```bash
gcloud config set project YOUR_PROJECT_ID
gcloud services enable cloudtasks.googleapis.com cloudfunctions.googleapis.com cloudbuild.googleapis.com
gcloud tasks queues create crm-queue --location=us-central1
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
    --member="serviceAccount:YOUR_PROJECT_ID@appspot.gserviceaccount.com" \
    --role="roles/cloudtasks.enqueuer"
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
    --member="serviceAccount:YOUR_PROJECT_ID@appspot.gserviceaccount.com" \
    --role="roles/run.invoker"
```

### 1.3 Enable Firebase Authentication

1. In the Firebase console → **Build → Authentication** → Get started
2. Go to **Sign-in method** and enable **Google** and **Email/Password**
3. Under **Settings → Authorised domains**, confirm your hosting domain is listed
   (Firebase automatically adds `*.firebaseapp.com` and `*.web.app`)

### 1.4 Create the web app and save the config

1. In the Firebase console → Project settings (gear icon) ��� **Your apps** → **Add app** → Web (`</>`)
2. Register the app (nickname e.g. "CRM frontend"), then copy the `firebaseConfig` object shown
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
5. **`firebase-config.js` is gitignored** — never commit it. The template `firebase-config.example.js` is committed instead.

### 1.5 Download the service account key

1. In Firebase console → Project settings → **Service accounts**
2. Click **Generate new private key** → save the JSON file
3. Place it at `config/serviceAccountKey.json`
4. **Never commit this file** — it is listed in `.gitignore`

---

## 2. Environment variables

Copy the example file and fill in your values:

```bash
cp .env.example .env
```

Edit `.env`:

```ini
# Firebase / Firestore
FIREBASE_CREDENTIALS=config/serviceAccountKey.json
FIRESTORE_COLLECTION=leads

# OpenAI — required for AI enrichment
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini

# Brave Search — required for contact enrichment
BRAVE_API_KEY=...

# Google APIs — for Custom Search (optional, Bing is primary)
GOOGLE_API_KEY=
GOOGLE_CSE_ID=

# GitHub token — improves rate limits (optional, no scopes needed)
GITHUB_TOKEN=

# Crawler tuning (defaults are good for most cases)
MAX_RESULTS=200
CRAWL_WORKERS=20
LIMIT_PER_HOST=3
CRAWL_DELAY=1.0
```

**Required API keys:**

| Key | Where to get it | Used by |
|---|---|---|
| `OPENAI_API_KEY` | [platform.openai.com](https://platform.openai.com) | All AI enrichment scripts |
| `BRAVE_API_KEY` | [brave.com/search/api](https://brave.com/search/api) | Contact enrichment |
| Firebase service account | Firebase console → Service accounts | All Firestore access |

**Optional but recommended:**

| Key | Where to get it | Used by |
|---|---|---|
| `GITHUB_TOKEN` | GitHub → Settings → Developer tokens | Agency discovery via GitHub |
| `GOOGLE_API_KEY` + `GOOGLE_CSE_ID` | Google Cloud Console → Custom Search | Supplementary search |

---

## 3. Python environment (local scripts)

The local pipeline scripts (`app/`) use a separate virtual environment from the Cloud Function.

```bash
# Create virtual environment
python -m venv .venv

# Activate (Linux/Mac)
source .venv/bin/activate

# Activate (Windows)
.venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

Verify the setup:
```bash
python -c "import firebase_admin, openai; print('OK')"
```

---

## 4. Google Sheets setup

The CRM workflow requires two Google Sheets:

### 4.1 Contact sheet (master CRM sheet)
- Create a Google Sheet
- Name the first tab **contacts**
- Required columns: `doc_id`, `email`, `name`, `title`, `website`, `country`, `campaign`, `status`, `select`
- Copy the sheet ID from the URL (the long alphanumeric string after `/spreadsheets/d/`)
- Set in `functions-crm/crm/sheets_config.py` or via env var: `CONTACT_SHEET_ID=your_sheet_id`

### 4.2 CRM work sheet
- Create a second Google Sheet
- Name the first tab **Outreach**
- Required columns will be written by `crm/push_and_sync.py` on first run
- Set `TEMPLATE_SHEET_ID=your_sheet_id`

### 4.3 Share both sheets with the service account

Go to each sheet → Share → paste the service account email:
```
YOUR_PROJECT_ID@appspot.gserviceaccount.com
```
Give it **Editor** access.

---

## 5. Cloud Function deployment

The CRM API runs as a Firebase Cloud Function from the `functions-crm/` directory.

### 5.1 Login to Firebase

```bash
firebase login
firebase use YOUR_PROJECT_ID
```

### 5.2 Deploy using the included script

Linux/Mac:
```bash
./deploy_crm.sh
```

Windows:
```bat
deploy_crm.bat
```

Or manually:

```bash
# Create venv for the Cloud Function
python -m venv functions-crm/venv
functions-crm/venv/bin/pip install -r functions-crm/requirements.txt

# Deploy
firebase deploy --only functions
firebase deploy --only hosting
firebase deploy --only firestore
```

### 5.3 Verify deployment

The API base URL will be:
```
https://us-central1-YOUR_PROJECT_ID.cloudfunctions.net/crmApi
```

Test it:
```bash
curl https://us-central1-YOUR_PROJECT_ID.cloudfunctions.net/crmApi/api/crm/jobs
```

### 5.4 Update `crm-common.js`

Edit `public/js/crm-common.js` and update the `BASE` constant to your API URL:

```js
const BASE = 'https://us-central1-YOUR_PROJECT_ID.cloudfunctions.net/crmApi';
```

Redeploy hosting after this change:
```bash
firebase deploy --only hosting
```

---

## 6. Firestore indexes

Deploy the Firestore indexes (required for collection group queries):

```bash
firebase deploy --only firestore
```

This deploys both `firestore.indexes.json` and `firestore.rules`.

---

## 7. Google Drive folder (optional)

The campaign export feature uploads spreadsheets to a Google Drive folder.

1. Create a folder in Google Drive
2. Share it with the service account email (Editor access)
3. Copy the folder ID from the URL
4. Open the CRM dashboard → **Settings** → paste the folder ID → **Save folder** → **Check access**

---

## 8. Mail accounts

Configure outreach email accounts in the CRM dashboard:

1. Open **Settings** → **Mail accounts** → **Add account**
2. Choose **IMAP** or **Google / Gmail**
3. For IMAP: fill in host, port, username, password, and SMTP settings
4. For Gmail: fill in Client ID, Client Secret, and Refresh Token (obtained from Google Cloud Console → OAuth 2.0)
5. Click **Test** to verify the connection

Gmail OAuth2 setup:
1. Google Cloud Console → APIs & Services → Credentials
2. Create OAuth 2.0 Client ID (Desktop app type)
3. Download the JSON → run the OAuth flow to get a refresh token
4. Paste the credentials into the mail account settings

---

## 9. First run checklist

Once everything is set up, verify the pipeline works end to end:

```bash
# 1. Test Firestore connection
python -c "
import _pathsetup
from functions.firebase_cred import get_firebase_cred
import firebase_admin
from firebase_admin import firestore
firebase_admin.initialize_app(get_firebase_cred())
db = firestore.client()
print('Firestore OK — leads count:', sum(1 for _ in db.collection('leads').limit(1).stream()))
"

# 2. Test OpenAI connection
python -c "
from functions.config import cfg
import openai
client = openai.OpenAI(api_key=cfg.OPENAI_API_KEY)
r = client.chat.completions.create(model=cfg.OPENAI_MODEL, messages=[{'role':'user','content':'ping'}], max_tokens=5)
print('OpenAI OK:', r.choices[0].message.content)
"

# 3. Run a small site_agent dry run
python app/site_agent.py --countries NO --max-results 5 --dry-run

# 4. Build filter facets
python app/build_filter_facets.py --no-write

# 5. Open the dashboard
# Navigate to: https://YOUR_PROJECT_ID.web.app
```

---

## 10. Project structure

```
collect_power_agent/
├── app/                    # Local Python pipeline scripts
│   └── functions/          # Shared utilities (config, firebase, utils)
├── config/                 # API keys, query files, blocklists
│   ├── serviceAccountKey.json  ← not committed
│   ├── site_agent_queries.json
│   ├── countries.json
│   ├── catalogs.json
│   ├── blocklist_domains.txt
│   └── site_agent_blocklist.txt
├── crm/                    # Local CRM sync scripts
├── functions-crm/          # Cloud Function (Flask API)
│   ├── crm/                # CRM libraries
│   ├── main.py             # API routes + worker
│   └── requirements.txt
├── public/                 # Frontend (Firebase Hosting)
│   ├── firebase-config.js          ← not committed (copy from example)
│   ├── firebase-config.example.js  ← template, commit this
│   ├── js/crm-common.js            # Shared nav + helpers
│   ├── js/auth.js                  # Firebase Auth guard + helpers
│   └── *.html
├── .env                    ← not committed
├── .env.example
├── firebase.json
├── firestore.indexes.json
├── firestore.rules
├── setup_gcp.sh / .bat
└── deploy_crm.sh / .bat
```

---

## Troubleshooting

**`No module named 'functions'`** — run scripts from the project root, not from `app/`. All scripts use `_pathsetup.py` to add the right paths.

**`firebase_admin.initialize_app` error** — check that `config/serviceAccountKey.json` exists and matches the correct project.

**Cloud Function returns 403** — the service account needs `roles/run.invoker` and `roles/cloudtasks.enqueuer` (run `setup_gcp.sh`).

**Sheets not found (404)** — verify the sheet IDs in `sheets_config.py` and that the service account has Editor access 
---

## 11. Access control — first admin setup

The system requires every user to be assigned a role before they can access any internal page or make any write API call. A signed-in user with no role is a **guest** — they can only see the landing page.

For full details see [`readme-access.md`](../../readme-access.md).

### Role hierarchy

| Role | What they can do |
|---|---|
| `guest` | View the landing page only. Cannot access any internal page or API write. |
| `user` | Full read access + follow-up field updates and email sync. |
| `campaign-user` | Everything `user` can do + create / manage campaigns and jobs. |
| `admin` | Full access including mail account settings and user management. |

### Assigning the first admin

There is no UI for the very first user — assign the role directly in Firestore:

1. Sign in to the CRM dashboard with the account that should be the first admin.
2. In [Firebase Console → Firestore](https://console.firebase.google.com) open:
   ```
   settings → users → users → {your-email-address}
   ```
3. Add a field:
   ```
   role: "admin"   (string)
   ```
4. Reload the dashboard — the account now has full access.

All subsequent users can be assigned roles via the **Settings → Users** page.

### Assigning roles to new users

1. A new user signs in — Firebase Authentication creates their account.
2. They land on the dashboard and see *"Your account is pending access — contact an administrator."*
3. An admin opens **Settings → Users**, finds the new user, and assigns a role.
4. The user refreshes — they now have access.

### How it is enforced

**Frontend:** `crm-common.js` checks `PAGE_ROLES` on every page load. Guests and unauthenticated users are redirected to the landing page automatically.

**Backend:** every API request (GET and non-GET) must carry a valid Firebase ID token in the `Authorization` header. The `before_request` hook in `functions-crm/main.py` verifies the token, fetches the role from Firestore, and returns `401` (no/bad token) or `403` (insufficient role) if the check fails.
