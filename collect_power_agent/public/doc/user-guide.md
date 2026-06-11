# Blueboot CRM — User Guide

## Overview

Blueboot CRM is an outreach pipeline system for discovering, qualifying, and contacting leads from the web. It uses lead discovery and site analysis pipelines that converge into a unified outreach contact list and campaign system.

---

## Navigation

The top navigation bar gives access to all sections:

| Section | Purpose |
|---|---|
| **Campaigns** | Manage and run outreach campaigns |
| **Follow-up** | Cross-campaign follow-up tracker with inline editable status and comments |
| **CRM discover** | Manual discovery workflow — export contacts, review, push to CRM work sheet |
| **Daily Admin** | Day-to-day operational tools — see below |
| **Documentation** | User guides and system docs |

**CRM discover** is a direct link in the top navigation bar. **Follow-up** is a standalone link immediately to the left of it.

### Daily Admin

**Daily Admin** is the hub for routine operational work on the system. It groups the tools you use regularly to keep the pipeline running:

| Item | Purpose |
|---|---|
| **Statistics** | Aggregated pipeline statistics across all leads and sites |
| **Filter facets** | Configure and test lead filter criteria |
| **Drive Folder** | Browse files in the connected Google Drive folder |
| **Message Box** | Read emails from configured outreach mail accounts |
| **Jobs** *(admin)* | Monitor background job progress |
| **Cloud Batch** *(admin)* | Trigger and manage cloud batch processing runs |
| **Settings** *(admin)* | Mail accounts and Drive folder configuration |
| **Users** *(admin)* | Manage user accounts and roles |

Items marked *(admin)* are only visible to administrators.

---

## Two ways to create a campaign

There are two separate routes to building a campaign, and it is important to understand the difference.

### Route 1 — Filter facets (automated)

You define what kind of companies you want using filters (country, sector, company size, etc.), run a count to confirm the numbers, and click **Create campaign**. The system pulls the matching contacts directly from the internal contact pool and fills the campaign automatically. No spreadsheet is involved.

This is the faster route and works well when your target audience can be described by filter criteria. See the [From filter to campaign](filter-to-campaign.html) guide for a full walkthrough.

### Route 2 — Master CRM sheet (manual curation)

The master CRM contact sheet is a shared Google Spreadsheet that sits outside the system. It is populated through **CRM discover**, which imports a selection of contacts from the internal pool into the sheet so a person can review them, mark the ones worth pursuing in the **Select** column, and assign a campaign name in the **Campaign** column.

Once the sheet is filled and reviewed, clicking **Discover campaigns** on the Campaigns page reads the Campaign column, finds any campaign names that do not yet exist in the system, and creates those campaigns automatically — pulling in all contacts assigned to that name in the sheet.

This route gives you full human control over exactly which contacts enter a campaign. It is slower but more precise, and is suited to smaller, high-priority lists where individual review matters.

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

Shows the campaign name. If the campaign has an associated Google Drive spreadsheet, a **Spreadsheet** link appears next to the name. On the right: status badge, source badge (if from master sheet), Sync, Full override, and Activate buttons.

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

### Campaign ↔ Sheet synchronisation

The campaign contact list and the Google Drive spreadsheet are kept in sync automatically. The rules are:

**DB is the source of truth for the contact list.** Contacts are only added to a campaign through the app (via Create campaign from facet, or manual API). The sheet never adds new contacts to the DB — it only updates existing ones.

**Sheet wins for editable fields.** When you sync from the sheet, the values in the sheet overwrite the DB for user-editable fields (name, title, last action, last action status, etc.). The DB always controls `status` and `sent_at`. Any new column you add to the sheet is automatically written to the DB as a new field on the contact doc.

**Campaign removals are propagated to the sheet.** When contacts are removed from the campaign via the Exclude + Remove excluded flow, a `campaign-export` job is automatically enqueued, which regenerates the sheet from the current DB state. The contacts disappear from this campaign's sheet, but the underlying contact records remain in the database and can be picked up by another campaign later. Conversely, when you sync from the sheet and a sheet row's Doc ID is no longer in the campaign, the sync detects the discrepancy and re-exports the sheet to remove the orphaned row.

### Sync button

Reads the campaign's Google Drive spreadsheet → updates Firestore for existing contacts only. **Sheet wins for all non-system fields.** `status` and `sent_at` are always DB-controlled. New columns in the sheet are written to the DB. Sheet rows whose Doc ID no longer exists in the DB are cleaned up by triggering a full sheet regeneration. If no sheet exists yet, behaves like Full override (creates the sheet).

### Full override button

Overwrites the campaign spreadsheet completely from the database. A confirmation popup warns that manual edits (except Last action and Last action status) will be lost.

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

## Filter Facets

**URL:** `filter-facets.html` — accessible via **Daily Admin → Filter facets**

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
