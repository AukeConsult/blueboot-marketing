# CRM

Two-way sync between Firestore `email_contacts` and a Google Sheet, with a manual selection workflow.

## Files

| File | Purpose |
|---|---|
| `contact_sync.py` | Main script — export, append and sync contacts between sheet and Firestore |
| `gsheet_sync.py` | Low-level read/write helpers for any Google Sheet |
| `inspect_sheet.py` | One-off: creates the `contacts` tab and prints headers |

## Setup

1. Download an OAuth2 client secret from GCP Console → APIs & Services → Credentials → OAuth 2.0 Client IDs (Desktop app)
2. Save it as `config/google_oauth_client.json`
3. Enable the Google Sheets API in GCP Console → APIs & Services → Enabled APIs
4. Install dependencies: `pip install google-api-python-client google-auth-oauthlib`
5. First run opens a browser for Google consent — token is cached in `config/google_token.json`

## Google Sheet

**Sheet ID:** `1aMglV53NiMEArjld37HN5cxliyNRGzIP2mrM4kwlupA`
**Tab:** `contacts`

## Firestore Structure

```
crm/
  contact_select/          <- namespace document
    items/                 <- subcollection
      {doc_id}             <- one document per contact
        select: ""         <- filled manually from sheet
        campaign: ""       <- editable in sheet
        email: ...
        website: ...
        ...
```

## Sync Procedure

1. **Export** — reads `email_contacts` from Firestore, reads sheet first, appends only contacts not already present (matched by Doc ID), writes to sheet + Firestore `crm/contact_select/items`
2. **Sync back** — reads sheet (sheet has precedence for `Select` and `Campaign`), re-fetches fresh Firestore data, merges, writes back to sheet + Firestore

Sheet values for `Select` and `Campaign` are **never overwritten** by either operation.

## Commands

### Append new contacts to sheet

Reads `email_contacts`, skips Doc IDs already in sheet, appends only new rows.

```bash
python crm\contact_sync.py --countries NO
python crm\contact_sync.py --countries NO UK --status pending
python crm\contact_sync.py --countries NO --campaign NO_resellers_jun02
python crm\contact_sync.py --countries NO --max 500
```

| Flag | Default | Description |
|---|---|---|
| `--countries` | all | Space or comma separated country codes e.g. `NO UK` |
| `--campaign` | — | Filter by campaign tag |
| `--status` | — | Filter by status: `pending` / `approved` / `sent` |
| `--collection` | `email_contacts` | Source Firestore collection |
| `--tab` | `contacts` | Sheet tab name |
| `--max` | — | Cap number of new rows added (applied after sort) |

### Sync sheet <-> Firestore

Re-fetches fresh data for existing sheet rows, merges with sheet overrides, writes back to sheet and Firestore.

```bash
python crm\contact_sync.py --sync-back
python crm\contact_sync.py --sync-back --tab contacts
```

## Columns

| # | Column | Firestore field | Notes |
|---|---|---|---|
| 1 | Select | _(manual)_ | Sheet precedence |
| 2 | Campaign | `campaign` | Sheet precedence |
| 3 | Tier | `tier_label` | |
| 4 | Outreach | `outreach_priority` | Mapped: Direct / Strong / Role/Dept / Admin/Generic |
| 5 | Status | `status` | |
| 6 | Email | `email` | |
| 7 | Website | `website` | |
| 8 | Name | `name` | |
| 9 | Title | `title` | |
| 10 | Phone | `phone` | Stored as text |
| 11 | LinkedIn | `linkedin` | |
| 12 | Email Type | `email_type` | |
| 13 | Contact Role | `contact_type` | |
| 14 | Domain | `domain` | |
| 15 | Country | `country` | |
| 16 | Location | `location` | |
| 17 | City | `location_city` | |
| 18 | Region | `location_region` | |
| 19 | Platform | `ai_platform` | |
| 20 | Sector | `ai_sector` | |
| 21 | Client Base | `ai_client_base` | |
| 22 | Company Type | `ai_company_type` | |
| 23 | Pages | `page_count` | |
| 24 | Confidence | `ai_confidence` | |
| 25 | Summary | `ai_summary` | |
| 26 | Keywords | `keywords` | |
| 27 | Lead ID Site | `lead_id_site` | |
| 28 | Lead ID Leads | `lead_id_leads` | |
| 29 | Created | `created_at` | Date only |
| 30 | Doc ID | `doc_id` | Index field — used for deduplication and Firestore matching |
