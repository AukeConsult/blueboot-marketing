# Blueboot CRM — User Guide

## Overview

Blueboot CRM is an outreach pipeline system for discovering, qualifying, and contacting leads from the web. It has two parallel pipelines — a **legacy leads pipeline** and a **site leads pipeline** — that converge into a unified outreach contact list and campaign system.

---

## Navigation

The top navigation bar gives access to all sections:

| Section | Purpose |
|---|---|
| **Campaigns** | Manage and run outreach campaigns |
| **CRM** | Step-by-step workflow from import to outreach |
| **Jobs** | Monitor background job progress |
| **Data collect** → Statistics | Aggregated pipeline statistics |
| **Data collect** → Filter facets | Lead filter configuration |
| **Drive Folder** | Files in the connected Google Drive folder |
| **Mailbox** | Read emails from configured outreach accounts |
| **Settings** | Mail accounts and Drive folder configuration |

---

## Campaigns

**URL:** `campaigns.html`

Lists all outreach campaigns. Each campaign card shows status, contact count, site count, countries, and whether it was created from the master CRM sheet (shown as a green `master-sheet` badge).

### Actions

- **Discover new** — scans the master CRM contact sheet for campaign IDs not yet in the system, creates them, and runs a full CRM sync to populate their contacts. A **Master sheet** link sits to the left of the button for direct access.
- **Refresh** — reloads the list.
- **Filters** — search by name, filter by status (Draft / Do send / Sent / Cancelled) and owner.

### Campaign statuses

| Status | Meaning |
|---|---|
| `draft` | Being prepared, not ready to send |
| `dosend` | Ready — the Activate button becomes visible |
| `sent` | Activated and delivered |
| `cancelled` | Cancelled |

---

## Single campaign

**URL:** `campaign.html?campaign_id=X`

### Page header

Shows the campaign name. If the campaign has an associated Google Drive spreadsheet, a **Spreadsheet** link appears next to the name. On the right: status badge, source badge (if from master sheet), Sync, Full override, and Activate buttons.

### Status line

A compact one-line summary: **N contacts · N sites · N countries · N sent · updated DATE**

### Campaign details (expandable)

- **Email account** — dropdown of configured mail accounts. Changing this saves immediately and updates which account the campaign uses for outreach. An eye icon opens a read-only popup showing the account's IMAP/Gmail settings.
- **Owner** — auto-saves 1.2 s after typing.
- **Activated at** — shown once the campaign has been activated.

### Mail template (expandable)

Shows the From address, Subject, and a rendered preview of the email body. Supports both plain text and HTML templates.

- **Edit** — opens the campaign editor.
- **Send test** — opens a popup pre-filled with the campaign subject and body. Sends a test email via the configured mail account. HTML emails are CSS-inlined before sending to ensure compatibility with spam filters.

### Sync button

Reads the campaign's Google Drive spreadsheet → updates Firestore. **Sheet wins for all fields** except `status` and `sent_at` which are always DB-controlled. New DB contacts not yet in the sheet are appended automatically. If no sheet exists yet, behaves like Full override (creates the sheet).

### Full override button

Overwrites the campaign spreadsheet completely from the database. A confirmation popup warns that manual edits (except Last action and Last action status) will be lost.

### Activate button

Only visible when campaign status is `dosend`. Marks the campaign as sent and queues it for outreach delivery. Requires confirmation.

### Contacts table

Lists all campaign contacts with status, name, email, title, website, and sent date.

- **Exclude selector** — per-row dropdown. Selecting "Exclude" changes the contact's status badge locally.
- **Remove excluded** button — appears when any contacts are set to Exclude. Opens a confirmation popup, then permanently removes those contacts from the campaign in Firestore.
- **Search** — filters the list client-side.

---

## CRM workflow

**URL:** `crm-bp.html`

A step-by-step workflow panel:

| Step | Action |
|---|---|
| 1 | **Import contacts** — pull from Leads Database into the contact sheet. Choose country and size (min pages). |
| 2 | **Review & select** — open the contact sheet, mark contacts with the `Select` column. |
| 3 | **Push selected to CRM** — groups selected contacts by site, adds them to the CRM template. |
| 4 | **Work the CRM** — fill Status and Selger in the CRM template as you progress. |
| 5 | **Sync CRM to Leads Database** — pushes `crm_status`, `crm_sales_person`, `crm_date` back to the Leads Database. |
| 6 | **Campaign sync** — reads the master CRM contact sheet and syncs all campaigns to Firestore. New campaigns found in the sheet are created automatically. |

---

## CRM Sync

**URL:** `crm-sync.html`

Standalone page for triggering a full CRM sync from the master contact sheet. Optionally filter to a single campaign ID. Shows recent sync jobs with result summaries.

---

## Jobs

**URL:** `jobs.html`

Monitors all background jobs (imports, syncs, exports). Jobs auto-refresh every 5 seconds without collapsing expanded rows. Click any job card header to expand/collapse its result or error detail.

Job statuses: `queued` → `running` → `done` or `error`.

---

## Statistics

**URL:** `statistics.html`

Aggregated statistics across both pipelines, displayed in three tabs:

### Leads tab
- **Lead pipeline** — doc counts for `leads` and `leads_excluded`, top countries, exclusion rate.
- **Lead enrichment** — how many leads/contacts have been AI-classified, social-enriched, and email-checked.

### Site leads tab
- **Site lead pipeline** — doc counts for `site_leads` and `sites_excluded`, top countries by AI country and sector, page-size distribution, exclusion rate.
- **Site enrichment** — AI classification, location enrichment, Brave enrichment, and email-check completion rates.
- **Data quality** — leads with no sitemap, zero page count, or not classified; contacts missing name or with name/email mismatches.

### Common tab
- **Priority × country** — lead counts by priority for each country.
- **email_contacts funnel** — outreach contact list broken down by status, pipeline membership (site/leads/both), and email type.
- **Pipeline cross-coverage** — how many contacts appear in both pipelines vs only one.

### Collect statistics button

Triggers a background job that runs all aggregations and writes the results to Firestore. The page auto-refreshes when the job completes.

---

## Filter facets

**URL:** `filter-facets.html`

Displays and manages the filter facet catalog — the selectable values used by the lead filtering UI (platform, AI sector, country, page size, occupation, etc.). Built by running:

```
python app/build_filter_facets.py
```

---

## Drive Folder

**URL:** `gdisk.html`

Browse, upload, and delete files in the connected Google Drive folder. Supports drag-and-drop upload. The folder is configured in Settings.

---

## Mailbox

**URL:** `mailbox.html`

Reads emails from all folders (INBOX, Sent, Drafts, Trash, etc.) of a configured mail account. Select an account from the dropdown and choose how many messages per folder to load.

The message list shows: **Folder** badge, From, To, Subject (with a preview snippet), and Date. Click any row to open the full message body in a popup.

---

## Settings

**URL:** `settings.html`

### Mail accounts

Configure the outreach email accounts used by campaigns. Each account can be either IMAP or Gmail (OAuth2).

**IMAP account fields:**
- Email address, Display name
- IMAP: Host, Port, Username, Password, SSL/TLS
- SMTP (outgoing): Host, Port, SSL/TLS (used when sending)

**Gmail account fields:**
- Email address, Display name
- Client ID, Client Secret, Refresh Token, Access Token (auto-refreshed)

**Actions per account:**
- **Send (paper plane)** — opens a test email popup to send from that account.
- **Edit (pencil)** — opens the edit modal.
- **Delete (trash)** — removes the account.

The **Test** button in the edit modal pings the connection live before saving.

Mail accounts are keyed by email address and stored in `settings/mail_accounts/accounts/{email}`. Campaigns reference them by the `outreach_email_account` field — changing that field on the campaign page automatically looks up the matching settings.

### Google Drive folder

Paste a Drive folder ID (the part after `/folders/` in the URL) and click **Save folder**. Use **Check access** to verify the backend service account has read/write permissions. If access is missing, share the folder with that service account as Editor.

---

## Key concepts

### Two pipelines

| | Legacy leads | Site leads |
|---|---|---|
| Collection | `leads` | `site_leads` |
| Contacts | `contacts` (subcollection) | `site_contacts` (subcollection) |
| Excluded | `leads_excluded` | `sites_excluded` |
| Enrichment | AI, social, email check | AI, location, Brave, email check |

Both pipelines feed into `email_contacts` — the unified outreach contact list.

### Campaigns and the master sheet

Campaigns are discovered from the master CRM contact sheet (a Google Sheet with a `Campaign` column). Each unique campaign ID in the sheet becomes a campaign document in Firestore. Contacts are synced per campaign.

The campaign's Google Drive spreadsheet is separate — it's the working sheet for follow-up tracking. Syncing between the Drive sheet and Firestore is bi-directional: **Export / Full override** goes DB→Sheet, **Sync** goes Sheet→DB (sheet wins, except `status` and `sent_at`).

### Mail sending

All outbound email goes through the `MailSender` class which:
- Inlines CSS via `premailer` (required for HTML email — Gmail, Outlook, and MailChannels strip `<style>` blocks)
- Adds `Message-ID` and `Date` headers
- Formats the `From` header with display name
- Appends sent mail to the IMAP Sent folder automatically (IMAP accounts only; Gmail saves Sent natively)
- Supports both STARTTLS (port 587) and SSL (port 465)

### Background jobs

Long-running operations (syncs, exports, statistics collection) are queued as Cloud Tasks jobs. The Jobs page shows their status in real time. Job results and errors are stored in Firestore and visible by expanding the job card.
