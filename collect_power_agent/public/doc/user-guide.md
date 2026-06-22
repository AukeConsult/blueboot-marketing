# Blueboot CRM — User Guide

## Overview

Blueboot CRM is an outreach pipeline system for discovering, qualifying, and contacting leads from the web. It uses lead discovery and site analysis pipelines that converge into a unified outreach contact list and campaign system.

---

## Navigation

The top navigation bar gives access to all sections:

| Section | Purpose |
|---|---|
| **Campaigns** | Dropdown — Campaign list, Import campaign, Filter facets |
| **Follow-up** | Cross-campaign follow-up tracker with inline editable status and comments |
| **CRM discover** | Manual discovery workflow — export contacts, review, push to CRM work sheet |
| **Daily Admin** | Day-to-day operational tools — see below |
| **Documentation** | User guides and system docs |

**Campaigns** is a dropdown containing the campaign list, the import tool, and filter facets. **CRM discover** and **Follow-up** are standalone links immediately to the right of it.

### Daily Admin

**Daily Admin** is the hub for routine operational work on the system. It groups the tools you use regularly to keep the pipeline running:

| Item | Purpose |
|---|---|
| **Statistics** | Aggregated pipeline statistics across all leads and sites |
| **Drive Folder** | Browse files in the connected Google Drive folder |
| **Message Box** | Read emails from configured outreach mail accounts |
| **Jobs** *(admin)* | Monitor background job progress |
| **Cloud Batch** *(admin)* | Trigger and manage cloud batch processing runs |
| **Settings** *(admin)* | Mail accounts and Drive folder configuration |
| **Users** *(admin)* | Manage user accounts and roles |

Items marked *(admin)* are only visible to administrators.

---

## Three ways to fill a campaign

There are three separate routes to building a campaign. Each suits a different situation — pick the one that matches how your data arrives.

| Route | Best for | Speed | Data source |
|---|---|---|---|
| **1 — Filter facets** | Audience defined by criteria (country, sector, size…) | Fastest | Internal contact pool |
| **2 — Excel import** | Pre-built lists, external data, or re-importing exports | Fast | Your own `.xlsx` file |
| **3 — Discover (master sheet)** | Human-curated contacts reviewed in a shared spreadsheet | Slower | Master CRM Google Sheet |

---

### Route 1 — Filter facets

**Where:** Campaigns → Filter facets

You define what kind of companies you want using filters (country, sector, platform, etc.), save the criteria as a named facet, and click **Create campaign from filter**. The system pulls all matching leads and contacts directly from the internal pool and fills the campaign automatically. No spreadsheet is involved.

**Steps:**
1. Go to **Campaigns → Filter facets**.
2. Adjust filters — each card is one dimension (country, company type, etc.).
3. Type a facet name and click **Save & count** to see how many contacts match.
4. Click **Create campaign from filter**, enter a campaign ID, and confirm.

This is the fastest route and works well when your target audience can be described by filter criteria. See the [From filter to campaign](doc-viewer.html?doc=filter-to-campaign) guide for a full walkthrough.

---

### Route 2 — Excel import

**Where:** Campaigns → Import campaign

You prepare a `.xlsx` file with the standard column layout (or export an existing campaign and edit it), then upload it. The system creates the campaign if it does not exist and writes all leads and contacts from the file. Existing outreach state is never overwritten.

**Steps:**
1. Go to **Campaigns → Import campaign**.
2. Click **Download example file** to get the column template if you are starting from scratch.
3. Fill in the spreadsheet — at minimum a **Website** or **Email** per row, and a **Campaign** column value.
4. Enter the campaign ID and drop the file onto the upload area.
5. Leave **Dry run** checked and click **Import** to preview counts — nothing is written yet.
6. Click **Confirm and write to Firestore** once the preview looks correct.

**What is safe to re-import:** you can re-import the same file repeatedly. Lead and contact profile fields are updated; outreach state (status, send history, follow-up fields) is never touched on existing contacts.

See [Campaign Import & Export](doc-viewer.html?doc=campaign-import-export) for the full column reference and CLI usage.

---

### Route 3 — Discover (master CRM sheet)

**Where:** Campaigns page → Discover campaigns button

The master CRM contact sheet is a shared Google Spreadsheet that lives outside the system. It is populated through **CRM discover**, where contacts from the internal pool are exported for human review. A person marks which contacts to keep (the **Select** column) and assigns a campaign name (the **Campaign** column).

Once the sheet is ready, clicking **Discover campaigns** reads the Campaign column, creates any campaign IDs that do not yet exist in the system, and immediately queues a sync job that pulls leads and contacts for each new campaign.

**Steps:**
1. Go to **CRM discover** and run an export to push contacts into the master sheet.
2. Open the master sheet, review each row, fill in the **Select** and **Campaign** columns.
3. Return to the **Campaigns** page and click **Discover campaigns**.
4. New campaigns are created automatically and a sync job is queued for each one.
5. Monitor progress on the **Jobs** page (admin only).

**What Discover does and does not do:**
- Creates new campaigns found in the sheet — never modifies existing ones.
- Writes leads and contacts from the sheet into each new campaign (create or update only — nothing is deleted).
- Existing outreach state on any contact is never overwritten.

**A campaign card on the list shows a green `master-sheet` badge when it was created through this route.**

---

## Campaigns

**URL:** `campaign.html`

The campaign workspace uses a full-page split layout. The left sidebar lists campaigns and scrolls independently. The main work area shows the selected campaign: campaign details and mail schedule on the left, and a right-hand work column that switches between the contact list and the mail editor.

### Actions

- **Campaign sidebar** — search by name, filter by status (Draft / Ready / Active / Canceled) and owner, then select a campaign to edit it on the right.
- **Refresh** — reloads the sidebar list.
- **Discover campaigns** — scans the master CRM contact sheet for any campaign IDs that do not yet exist in the system, creates them, and immediately kicks off a contact sync for each one. Before using this route, make sure you have updated and selected the contacts you want in **CRM Discover**.

### How Discover campaigns works

The master CRM contact sheet is the central spreadsheet that holds all contacts across all campaigns. Each row in the sheet has a **Campaign** column that contains a campaign identifier (for example `NO_jun` or `SE_aug`).

When you click **Discover campaigns**, the system:

1. Reads every unique value in the Campaign column of the master sheet.
2. Compares that list against the campaigns that already exist in the system.
3. For each campaign ID found in the sheet but not yet in the system, creates a new draft campaign automatically.
4. Immediately queues a contact sync job for each new campaign — this reads the sheet and pulls the matching rows into the campaign's contact list.

A confirmation bar appears at the top of the page showing which campaign IDs were created and confirming that the sync jobs have been queued. You can monitor progress on the **Jobs** page.

**When to use it:** after the master CRM sheet has been updated with contacts assigned to a new campaign ID that has not been set up in the system yet. You do not need to create the campaign manually first — Discover campaigns handles that in one click.

**Nothing is overwritten.** Campaigns that already exist are never touched. Only genuinely new campaign IDs (ones the system has never seen before) result in new campaign documents being created.

### Automatic campaign naming

When a campaign is created — whether from a filter preset, via Discover campaigns, or directly — the system checks whether the requested name already exists. If it does, a number is appended automatically: `NO_jun` becomes `NO_jun_2`, then `NO_jun_3`, and so on. You are never asked to choose a different name yourself; the system resolves the conflict silently and shows you what name was actually used in the confirmation message.

### Campaign statuses

| Status | Meaning |
|---|---|
| `draft` | Being prepared, not ready to send |
| `ready` | Reviewed and ready to send |
| `active` | First real mail has been sent; campaign remains active until canceled |
| `canceled` | Stopped; can be deleted |

---

## Single campaign

**URL:** `campaign.html?campaign_id=X`

The same campaign workspace opens with campaign `X` preselected in the left sidebar.

### Page header

Shows the campaign name. If the campaign has an associated Google Drive spreadsheet, a **Spreadsheet** link appears next to the name. On the right: status badge, source badge (if from master sheet), Full override, and Activate buttons.

### Status line

A compact one-line summary: **N contacts · N sites · N countries · N active · updated DATE**

### Campaign details (expandable)

- **Email account** — dropdown of configured mail accounts. Changing this saves immediately and updates which account the campaign uses for outreach. An eye icon opens a read-only popup showing the account's IMAP/Gmail settings.
- **Owner** — auto-saves 1.2 s after typing.
- **Active since** — shown once the campaign has sent its first real mail.
- **Built from facet filter** — shown when the campaign was created from a filter-facets preset. Displays the preset name (linked to `filter-facets.html`), the timestamp it was last built, and each active filter field as a pill badge (e.g. `ai_company_type: b2b`, `email_type: personal`). Updated every time the facet-campaign job runs.

### Mail schedule and editor

The **Mail schedule** section lists the outreach steps for the campaign, such as Intro, Reminder 1, and Reminder 2. Each step shows its day offset, subject, sent state, and quick action buttons.

The campaign schedule is shared by all contacts, but each contact moves through it on its own clock. Every automatic send appends to that contact's `mail_sent` history. The next step is chosen from how many mails that specific contact has already received, and the day offset is counted from that contact's first sent mail.

Example: if Follow-up 1 is Day 7, contacts that received Intro on different days will also receive Follow-up 1 on different days.

- **Add step** — creates a new schedule step and opens the mail editor in the right-hand work column.
- **Edit step** — opens the selected step in the right-hand mail editor. The contact list is hidden while you edit.
- **Contacts** — in the editor header, switches the right-hand work column back to the contact list.
- **Send test** — opens a popup pre-filled with that step's subject and body. Sends a test email via the configured mail account. HTML emails include the stored CSS.

The mail editor supports plain text and HTML, autosaves changes, has an explicit save button, and can preview the rendered body with sample placeholder values.

### Full override button

Overwrites the campaign spreadsheet completely from the database. Creates the sheet if it does not exist yet. The sheet has two tabs: **Leads+Contacts** (one row per contact, including all lead fields) and **Summary** (campaign metadata, country and platform breakdown). A confirmation popup warns that any manual edits to the sheet will be lost.

### Mark ready button

Only visible when campaign status is `draft`. Marks the campaign as `ready`. The campaign becomes `active` automatically after the first real outreach mail is sent.

### Delete button

Only visible when campaign status is `draft` or `canceled`. Opens a confirmation popup showing the contact count. On confirm, atomically marks the campaign as `deleting` (Firestore transaction) and enqueues a background `campaign-delete` job that batch-deletes all `campaign_contacts` then the campaign document. The workspace reloads the sidebar and selects the next campaign on completion.

### Contacts table

Lists all campaign contacts with contact status, name, email, title, and website.

- **Active button** — sets a contact's lifecycle status directly to `active` in Firestore.
- **Exclude button** — toggles a contact between `excluded` and `pending` directly in Firestore.
- **Remove excluded** button — appears when any contacts are set to Exclude. Opens a confirmation popup explaining that the action removes the contacts only from this campaign, not from the database. Removing them frees those email addresses so other campaigns can pick them up later. Keeping them excluded in this campaign keeps the email reserved here, so it will not appear in other campaigns.
- **Search** — filters the list client-side.

---

## CRM Batch Process

**URL:** `crm-bp.html` — accessible via **CRM discover** in the top navigation bar

This is the manual curation workflow that fills the **master CRM contact sheet** — the Google Spreadsheet used as the starting point for the master-sheet campaign route (see above).

| Step | Action |
|---|---|
| 1 | **Export contacts** — pull a selection from the internal contact pool into the master sheet. Choose country and minimum site size to narrow the import. |
| 2 | **Review & select** — open the master sheet, review each row, and mark the contac
---

## Campaign Import

**URL:** `campaign-import.html` — accessible via **Campaigns → Import campaign**

The import page loads leads and contacts into a campaign from a standard `.xlsx` file — either one exported from the system or one you have prepared manually.

### Download the example template

Click **Download example file** below the upload area to get an empty `.xlsx` with the correct column layout. It contains two tabs:

- **Leads+Contacts** — the 20 column headers with two sample rows you can delete.
- **Field guide** — a description, example value, and required/optional label for every column.

### Importing

1. Enter the campaign ID (it will be created as a draft if it does not exist).
2. Drop the `.xlsx` file onto the upload area or click **Browse file**.
3. Leave **Dry run** checked and click **Import** to preview counts — nothing is written yet.
4. Review the preview (leads new, leads updated, contacts new, contacts updated, skipped).
5. Click **Confirm and write to Firestore** to apply.

Uncheck **Dry run** to skip the preview and write immediately.

**Protected fields:** importing never overwrites outreach state on existing contacts — status, send history, and follow-up fields are always preserved.

See [Campaign Import & Export](doc-viewer.html?doc=campaign-import-export) for the full column reference and CLI usage.

---

## Filter Facets

**URL:** `filter-facets.html` — accessible via **Campaigns → Filter facets**

Filter Facets is where you define and save the audience criteria used to build campaigns automatically. A saved set of criteria is called a **facet**.

### Loading a facet

Pick an existing facet from the **Load facets** dropdown. The filters update immediately to reflect the saved selection. Use the refresh icon next to the dropdown to reload the list if you have just saved a new facet elsewhere.

### Adjusting filters

Each card represents one filter dimension (country, sector, company size, etc.). Check or uncheck values to narrow or broaden the audience. The selection summary bar at the top shows how many values are selected across how many categories, and updates as you make changes.

Use **Show selected only** in the summary bar to focus on what is active. **Clear all** resets every filter in one click.

### Saving a facet

Type a name in the **Facet name** field and click **Save & count**. Type a new name to create a new facet — type the name of an existing facet to overwrite it. After saving, the system runs a contact count and shows how many leads and contacts match the current filters.

### Creating a campaign from a facet

Once a facet has been saved and counted, the **Create campaign from filter** button becomes active in the selection summary bar. Click it to open the campaign creation dialog, enter a campaign ID, and confirm. The system pulls all matching contacts into a new campaign automatically. If the campaign ID already exists, its contact list is refreshed instead of creating a new one.

Tick **Dry run** to see the contact count without writing anything.
