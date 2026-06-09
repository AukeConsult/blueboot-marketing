# Blueboot CRM — User Guide

## Overview

Blueboot CRM is an outreach pipeline system for discovering, qualifying, and contacting leads from the web. It has two parallel pipelines — a **legacy leads pipeline** and a **site leads pipeline** — that converge into a unified outreach contact list and campaign system.

---

## Navigation

The top navigation bar gives access to all sections:

| Section | Purpose |
|---|---|
| **Campaigns** | Manage and run outreach campaigns |
| **Follow-up** | Cross-campaign follow-up tracker with inline editable status and comments |
| **CRM discover** | Manual discovery workflow — export contacts, review, push to CRM work sheet |
| **Jobs** | Monitor background job progress |
| **Data collect** → Statistics | Aggregated pipeline statistics |
| **Data collect** → Filter facets | Lead filter configuration |
| **Drive Folder** | Files in the connected Google Drive folder |
| **Mailbox** | Read emails from configured outreach accounts |
| **Settings** | Mail accounts and Drive folder configuration |

**CRM discover** is a direct link in the top navigation bar. **Follow-up** is a standalone link immediately to the left of it.

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

**URL:** `campaigns.html`

Lists all outreach campaigns. Each campaign card shows status, contact count, site count, countries, and whether it was created from the master CRM sheet (shown as a green `master-sheet` badge).

### Actions

- **Discover campaigns** — scans the master CRM contact sheet for any campaign IDs that do not yet exist in the system, creates them, and immediately kicks off a contact sync for each one. Before clicking this, make sure you have updated and selected the contacts you want in **CRM Discover**.
- **Refresh** — reloads the list.
- **Filters** — search by name, filter by status (Draft / Do send / Sent / Cancelled) and owner.

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

**URL:** `crm-bp.html` — accessible via **CRM discover** in the top navigation bar

This is the manual curation workflow that fills the **master CRM contact sheet** — the Google Spreadsheet used as the starting point for the master-sheet campaign route (see above).

| Step | Action |
|---|---|
| 1 | **Export contacts** — pull a selection from the internal contact pool into the master sheet. Choose country and minimum site size to narrow the import. |
| 2 | **Review & select** — open the master sheet, review each row, and mark the contac