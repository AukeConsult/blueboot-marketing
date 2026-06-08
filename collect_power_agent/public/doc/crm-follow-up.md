# CRM Follow-up

## Overview

The **CRM Follow-up** page gives you a single cross-campaign view of all contacts, so you can track the status of your outreach follow-up without having to open each campaign separately.

Access it from the navigation bar: **CRM → Follow-up**

---

## What it shows

The page loads every document in the `campaign_contacts` Firestore collection group — contacts from all campaigns at once. For each contact it also pulls the parent campaign's **owner** and **outreach email** so you can filter by those without opening individual campaigns.

Up to 2 000 contact documents are loaded per refresh.

---

## Filters

| Filter | What it does |
|---|---|
| **Search** | Free-text match on name, email, website, campaign ID, title, and owner |
| **Owner** | Show only contacts belonging to campaigns owned by a specific person |
| **Outreach email** | Show only contacts in campaigns that use a particular outreach account |
| **Follow-up status** | Filter by the follow-up status you have set on each contact (see below) |
| **Contact status** | Defaults to **Open contacts** (excludes sent); switch to All, Pending only, Sent only, or Excluded only |

All filters combine — you can, for example, show only contacts owned by a specific person with no follow-up status set yet.

---

## Follow-up fields

Each row has three editable follow-up fields. Changes are written **directly to Firestore** as soon as you leave the field — no Save button needed. A small blue dot pulses next to the field while it is saving; it disappears when the write confirms. If the save fails the dot turns red and shows the error in a tooltip.

| Field | Type | Description |
|---|---|---|
| **Follow-up date** | Date | The date you intend to follow up, or the date you last followed up |
| **Follow-up status** | Dropdown | The current stage of the follow-up (see statuses below) |
| **Comment** | Text | Free-text notes — a short reminder or outcome |

### Follow-up statuses

| Status | Meaning |
|---|---|
| *(none)* | Not yet actioned |
| **Open** | On your radar, not yet contacted again |
| **Contacted** | You have sent a follow-up |
| **Replied** | The contact has replied |
| **Meeting booked** | A meeting or call has been scheduled |
| **Closed** | Converted, won, or otherwise resolved |
| **Not interested** | The contact declined or is not a fit |

---

## Columns

| Column | Source |
|---|---|
| **Name / Title** | Contact name and job title from the campaign contact doc |
| **Email** | Clickable `mailto:` link |
| **Website** | Links to the contact's site (opens in a new tab) |
| **Campaign** | Links to the campaign page (`campaign.html?campaign_id=…`) |
| **Owner** | The campaign owner |
| **Outreach email** | The email account used for outreach in that campaign |
| **Follow-up date** | Editable — stored in Firestore on the contact doc |
| **Follow-up status** | Editable — stored in Firestore on the contact doc |
| **Comment** | Editable — stored in Firestore on the contact doc |

---

## Data storage

The three follow-up fields (`followup_date`, `followup_status`, `followup_comment`) are stored directly on each contact document inside the campaign's `campaign_contacts` subcollection:

```
campaigns/{campaign_id}/campaign_contacts/{doc_id}
```

They are written using a Firestore field-mask PATCH so only the changed field is updated — other contact fields (email, name, status, etc.) are never touched.

---

## Refreshing

Click **Refresh** in the top-right corner to reload all contacts from Firestore. The filter state is preserved across refreshes.
