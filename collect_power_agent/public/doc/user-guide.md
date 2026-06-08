# Blueboot CRM — User Guide

## Overview

Blueboot CRM is an outreach pipeline system for discovering, qualifying, and contacting leads from the web. It has two parallel pipelines — a **legacy leads pipeline** and a **site leads pipeline** — that converge into a unified outreach contact list and campaign system.

---

## Navigation

The top navigation bar gives access to all sections:

| Section | Purpose |
|---|---|
| **Campaigns** | Manage and run outreach campaigns |
| **CRM** → Batch process | Step-by-step workflow from import to outreach |
| **CRM** → CRM Sync | Sync the master CRM sheet to Firestore |
| **CRM** → Follow-up | Cross-campaign follow-up tracker with inline editable status and comments |
| **Jobs** | Monitor background job progress |
| **Data collect** → Statistics | Aggregated pipeline statistics |
| **Data collect** → Filter facets | Lead filter configuration |
| **Drive Folder** | Files in the connected Google Drive folder |
| **Mailbox** | Read emails from configured outreach accounts |
| **Settings** | Mail accounts and Drive folder configuration |

The **CRM** entry in the navigation bar is a dropdown menu. Click it to expand the three sub-pages.

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
- **Built from facet filter** — shown when the campaign was created from a filter-facets preset. Displays the preset name (linked to `filter-facets.html`), the timestamp it was last built, and each active filter field as a pill badge (e.g. `ai_company_type: b2b`, `email_type: personal`). Updated every time the facet-campaign job runs.

### Mail template (expandable)

Shows the From address, Subject, and a rendered preview of the email body. Supports both plain text and HTML templates.

- **Edit** — opens the campaign editor.
- **Send test** — opens a popup pre-filled with the campaign subject and body. Sends a test email via the configured mail account. HTML emails are CSS-inlined before sending to ensure compatibility with spam filters.

### Campaign ↔ Sheet synchronisation

The campaign contact list and the Google Drive spreadsheet are kept in sync automatically. The rules are:

**DB is the source of truth for the contact list.** Contacts are only added to a campaign through the app (via Create campaign from facet, or manual API). The sheet never adds new contacts to the DB — it only updates existing ones.

**Sheet wins for editable fields.** When you sync from the sheet, the values in the sheet overwrite the DB for user-editable fields (name, title, last action, last action status, etc.). The DB always controls `status` and `sent_at`. Any new column you add to the sheet is automatically written to the DB as a new field on the contact doc.

**Deletes are propagated both ways.** When contacts are removed from the campaign (via the Exclude + Delete excluded flow, or bulk delete), a `campaign-export` job is automatically enqueued, which regenerates the sheet from the current DB state — deleted contacts disappear from the sheet. Conversely, when you sync from the sheet and a sheet row's Doc ID is no longer in the DB, the sync detects the discrepancy and re-exports the sheet to remove the orphaned row.

### Sync button

Reads the campaign's Google Drive spreadsheet → updates Firestore for existing contacts only. **Sheet wins for all non-system fields.** `status` and `sent_at` are always DB-controlled. New columns in the sheet are written to the DB. Sheet rows whose Doc ID no longer exists in the DB are cleaned up by triggering a full sheet regeneration. If no sheet exists yet, behaves like Full override (creates the sheet).

### Full override button

Overwrites the campaign spreadsheet completely from the database. A confirmation popup warns that manual edits (except Last action and Last action status) will be lost.

### Activate button

Only visible when campaign status is `dosend`. Marks the campaign as sent and queues it for outreach delivery. Requires confirmation.

### Delete button

Only visible when campaign status is `draft`. Opens a confirmation popup showing the contact count. On confirm, atomically marks the campaign as `deleting` (Firestore transaction) and enqueues a background `campaign-delete` job that batch-deletes all `campaign_contacts` then the campaign document. Redirects to the campaigns list on completion. Campaigns with any other status cannot be deleted.

### Contacts table

Lists all campaign contacts with status, name, email, title, website, and sent date.

- **Exclude selector** — per-row dropdown. Selecting "Exclude" changes the contact's status badge locally.
- **Remove excluded** button — appears when any contacts are set to Exclude. Opens a confirmation popup, then permanently removes those contacts from the campaign in Firestore.
- **Search** — filters the list client-side.

---

## CRM Batch Process

**URL:** `crm-bp.html` — accessible via **CRM → Batch process**

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

**URL:** `crm-sync.html` — accessible via **CRM → CRM Sync**

Standalone page for triggering a full CRM sync from the master contact sheet. Optionally filter to a single campaign ID. Shows recent sync jobs with result summaries.

---

## CRM Follow-up

**URL:** `crm_follow.html` — accessible via **CRM → Follow-up**

A cross-campaign follow-up tracker that loads every contact from every campaign in one view. Use it to manage ongoing outreach without switching between individual campaign pages.

For full details see the dedicated [CRM Follow-up guide](doc-viewer.html?doc=crm-follow-up).

### Filters

Filter contacts by owner, outreach email account, follow-up status, and contact status (defaults to open — excludes already-sent contacts). A free-text search matches name, email, website, title, and owner.

### Follow-up fields

Three inline-editable fields are shown per contact and saved directly to Firestore on change — no Save button needed:

| Field | Description |
|---|---|
| **Follow-up date** | The date you plan to or last followed up |
| **Follow-up status** | Open / Contacted / Replied / Meeting booked / Closed / Not interested |
| **Comment** | Free-text note about the contact |

### Comment history

Every time you update the comment field the previous value is appended to a `comment_history` array on the contact document in Firestore, recording the date, your user account, and the comment text. Click the **chevron button** to the right of the comment field to expand the history panel, which shows all past comments newest-first.

### Batch selection

Each row has a checkbox on the left. Selecting one or more rows shows a **batch bar** above the table with a count and a Clear button. Batch actions can be wired up to the selection in future.

### Sorting

All columns except Comment are sortable — click a column header to sort ascending, click again to sort descending.

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

Displays and manages the filter facet catalog — the selectable values used by the lead filtering UI (platform, AI sector, country, page size, occupation, etc.). The catalog is rebuilt periodically by a background process; a developer can also trigger it manually from the command line. See the [System Architecture](system-architecture.md) document for details.

### Toolbar

- **Load facets** — dropdown of saved presets. Switching presets clears the Save as field.
- **Load** — reloads the selected preset.
- **Save as / Save & count** — saves the current selections under a new preset name and enqueues a `filter-count` job. The job refreshes the keyword list, counts matching sites and contacts, and writes `selected_count` back onto every facet value.
- **Create campaign** — only enabled after a count job has confirmed `contacts_in_email_contacts > 0`. Opens a modal to enter a campaign ID and optional dry-run flag; enqueues a `facet-campaign` job. Rerun on an existing campaign refreshes matching contacts (preserves outreach history on non-pending contacts) and removes stale pending contacts.

### selected_count

After a count job completes, each facet value shows two numbers: **N / M** where N (blue) is `selected_count` — how many matched sites/contacts have that value — and M is the total across all sites. This lets you see the distribution of your filter results without leaving the page.

---

## Drive Folder

**URL:** `gdisk.html`

Browse, upload, and delete files in the connected Google Drive folder. Supports drag-and-drop upload. The folder is configured in Settings.

---

## Mailbox

**URL:** `mailbox.html`

Reads emails from all folders (INBOX, Sent, Drafts, Trash, etc.) of a configured mail account. Select an account from the dropdown and choose how many messages per folder to load.

The message list shows: **Folder** badge, From, To,