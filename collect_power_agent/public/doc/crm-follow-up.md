# CRM Follow-up

## Overview

The **CRM Follow-up** page gives you a single cross-campaign view of all contacts, so you can track the status of your outreach follow-up without having to open each campaign separately.

Access it from the navigation bar: **CRM → Follow-up**

---

## What it shows

The page loads every contact from all campaigns in one call to the CRM API (`GET /api/crm/followup-contacts`). For each contact the API also joins the parent campaign's **owner** and **outreach email**, so you can filter by those without opening individual campaigns.

Up to 2 000 contact documents are loaded per refresh.

---

## Filters

| Filter | What it does |
|---|---|
| **Search** | Free-text match on name, email, website, and title |
| **Owner** | Show only contacts belonging to campaigns owned by a specific person |
| **Outreach email** | Show only contacts in campaigns that use a particular outreach account |
| **Follow-up status** | Filter by the follow-up status set on each contact |
| **Importance** | Filter by importance level (High / Medium / Low / Not set) |
| **Contact status** | Defaults to **Open contacts** (excludes sent); switch to All, Pending only, Sent only, or Excluded only |

All filters combine — you can, for example, show only high-importance contacts owned by a specific person with no follow-up status set yet.

---

## Follow-up fields

Each row has four editable follow-up fields. Changes are saved through the CRM API as soon as you leave the field — no Save button needed. A small blue dot pulses while saving; it turns green on success or red on failure (with an error tooltip).

| Field | Type | Description |
|---|---|---|
| **Follow-up date** | Date | The date you intend to follow up, or the date you last followed up |
| **Follow-up status** | Dropdown | The current stage of the follow-up (see statuses below) |
| **Importance** | Dropdown | Priority level — Low, Medium, or High |
| **Comment** | Text | Free-text notes — a short reminder or outcome |

### Follow-up statuses

| Status | Meaning |
|---|---|
| *(none)* | Not yet actioned |
| **Open** | On your radar, not yet contacted again |
| **Contacted** | You have sent a follow-up |
| **Replied** | The contact has replied |
| **Meeting booked** | A meeting or call has been scheduled |
| **Offer sent** | A proposal or offer has been sent |
| **Accepted offer** | The contact has accepted the offer |
| **Closed** | Converted, won, or otherwise resolved |
| **Not interested** | The contact declined or is not a fit |

---

## Comment history

Every time you change a follow-up field the backend automatically appends a log entry to `comment_history` on the contact document. The entry records the date, your user account, the new value, and the type of change (STATUS, COMMENT, FOLLOWUP, IMPORTANCE).

Click the **chevron button** to the right of the comment field to expand the history panel. Entries are shown newest-first. Email sync entries (see below) appear as colour-coded **IN** / **OUT** badges.

---

## Email sync

The page can sync outbound and inbound emails with your contacts, so you can see your full communication history alongside the follow-up fields.

### Sync all contacts

1. Choose a **lookback window** from the period selector in the top-right (7 days by default).
2. Click **Sync all emails**.
3. A job is queued on the backend and a blue status line appears showing the job ID. When the job completes the status line turns green with a count of new entries, and the page reloads.

### Sync a single contact

Click the **mail-bolt icon** next to any contact name. The same job mechanism runs for that one contact only, and the status line shows the result.

### How it works

The backend job (`followup-email-sync`) connects to each outreach account via IMAP, fetches headers for inbox and sent folders within the selected date window, matches emails by address to contacts in Firestore, and appends `EMAIL_IN` / `EMAIL_OUT` entries to `comment_history`. The operation is idempotent — each entry carries a unique `email_id` (from the mail provider's `Message-ID` header) so re-syncing never creates duplicates.

---

## Batch: set follow-up date

Select one or more contacts using the checkboxes on the left. A batch bar appears showing the count and a **Set follow-up date** button. Enter a date and an optional comment, then click **Apply to N selected**. All selected contacts are updated in parallel via the API and the panel closes automatically on success.

---

## Sorting

All columns except Comment are sortable — click a column header to sort ascending, click again to sort descending.

---

## Refreshing

Click **Refresh** in the top-right corner to reload all contacts from the API. Filter and sort state is preserved across refreshes.
