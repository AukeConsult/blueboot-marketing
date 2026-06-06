# Architecture

## Backend API (functions-crm/main.py)
Flask app deployed as Firebase Function `crmApi`.

### Key endpoints
- GET  /api/crm/campaigns — list campaigns
- GET  /api/crm/campaigns/<id> — single campaign + mail_account from settings
- POST /api/crm/campaigns/<id> — update campaign fields
- GET  /api/crm/crm-sync — trigger master sheet → Firestore sync (job)
- GET  /api/crm/campaign-sync — trigger campaign Drive sheet ↔ Firestore sync (job)
- GET  /api/crm/discover-campaigns — find new campaigns in master sheet, run crm-sync
- GET  /api/crm/settings/mail-accounts — list mail accounts
- POST /api/crm/settings/mail-accounts — create/update mail account
- POST /api/crm/settings/mail-accounts/<email>/ping — test connection
- POST /api/crm/settings/mail-accounts/<email>/send-test — send test email
- GET  /api/crm/settings/mail-accounts/<email>/mailbox — read all folders

## Data model (Firestore)
- `campaigns/{id}` — campaign doc (status: draft|dosend|sent|cancelled)
  - `.campaign_contacts/{doc_id}` — contact subcollection
  - outreach_email_account links to mail account (NOT stored on campaign)
- `settings/mail_accounts/accounts/{email}` — IMAP/Gmail credentials
  - Fields: account_type, email, display_name, host, port, username, password, ssl
  - SMTP: smtp_host, smtp_port, smtp_ssl
  - Gmail: client_id, client_secret, refresh_token, access_token

## Campaign sync flows
- **CRM sync** (master sheet → DB): crm_sync_lib.run_crm_sync()
  - Reads CONTACT_SHEET_ID from sheets_config.py
  - Creates new campaigns, updates contacts, status="draft" default
  - Campaigns from this source tagged source="master-sheet"
- **Campaign sync** (Drive sheet ↔ DB): campaign_sync_lib.run_campaign_sync()
  - Sheet wins for all fields EXCEPT status, sent_at (DB-controlled)
  - New DB contacts appended to sheet
  - No sheet → delegates to export

## Mail system (crm/mail_sender.py)
Single MailSender class handles all sending:
- CSS inlining via premailer (removes <style> tags, inlines rules)
- Display name in From header: "Name <email>"
- Message-ID + Date headers added
- IMAP: SMTP with STARTTLS (587) or SSL (465), appends to Sent folder after send
- Gmail: OAuth2 XOAUTH2, Gmail saves Sent automatically

## Frontend pages
- campaigns.html — campaign list + Discover new (runs crm-sync)
- campaign.html — single campaign view, sync/full-override buttons
- crm-bp.html — CRM workflow steps 1-6 (includes campaign sync step 6)
- mailbox.html — reads all IMAP folders, shows folder/from/to/subject/date
- settings.html — mail accounts CRUD + Drive folder config
- crm-sync.html — standalone CRM sync page (also accessible from CRM page)
