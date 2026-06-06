# Project Overview

**Project:** collect_power_agent — Blueboot CRM outreach system
**Owner:** Leif Auke (leifauke@gmail.com)
**Stack:** Firebase Functions (Python 3.12) + Firebase Hosting (vanilla HTML/Bootstrap)
**Deployed:** https://blueboot-market.web.app/
**API base:** https://us-central1-blueboot-market.cloudfunctions.net/crmApi

## Key folders
- `functions-crm/` — Flask API (main.py) + helper libs in crm/
- `public/` — Frontend HTML pages + vendor CSS/JS
- `public/js/crm-common.js` — Shared nav, BASE url, helpers (DO NOT TRUNCATE)
- `crm/` — Python scripts for local CRM sync

## Current features built (June 2026)
- Campaign management: list, view, sync, export to Drive, activate
- Mail accounts: IMAP + Gmail OAuth2 settings, ping, send test, CSS inlining via premailer
- Mailbox page: reads all IMAP folders, shows from/to/folder per message
- Appends sent mail to IMAP Sent folder after sending
- CRM sync: master sheet → Firestore (crm_sync_lib.py), campaign Drive sheet ↔ Firestore (campaign_sync_lib.py)
- Firestore indexes: campaign_contacts collection group indexes added

## Critical files to check before editing
- `functions-crm/main.py` (large — use Python scripts for edits, not Edit tool)
- `public/js/crm-common.js` (truncation breaks ALL pages — BASE and nav live here)
- `functions-crm/crm/mail_sender.py` — single source for all outbound mail
