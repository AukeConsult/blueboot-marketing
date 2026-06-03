# CRM

Google Sheets + Firestore CRM pipeline for outreach tracking.

---

## Files

| File | Purpose |
|---|---|
| `config.py` | Sheet IDs and shared constants |
| `contact_sync.py` | Export `email_contacts` from Firestore → contact sheet (two-way sync) |
| `contact_to_template.py` | Push selected contacts from contact sheet → CRM template sheet + Firestore |
| `crm_template_sync.py` | Sync CRM template sheet → Firestore + enrich from `site_leads` + update `site_leads` CRM fields |
| `setup_outreach_sheet.py` | One-off: create a new Google Sheet with outreach CRM structure |
| `inspect_sheet.py` | One-off: create the contacts tab and print headers |
| `outreach_crm_template.xlsx` | Local Excel template matching the CRM sheet structure |

---

## Setup

1. Download an OAuth2 client secret from GCP Console → APIs & Services → Credentials → OAuth 2.0 Client IDs (Desktop app)
2. Save it as `config/google_oauth_client.json`
3. Enable Google Sheets API in GCP Console → APIs & Services → Enabled APIs
4. Install: `pip install google-api-python-client google-auth-oauthlib`
5. First run opens a browser for consent — token cached in `config/google_token.json`

---

## Google Sheets

| Sheet | ID | Tab |
|---|---|---|
| Contact Sheet | `1aMglV53NiMEArjld37HN5cxliyNRGzIP2mrM4kwlupA` | `contacts` |
| CRM Template | `1b1kGKIldeawESH3RYiYjOqRFXRR5kG_81qYRFZI1gSY` | `Outreach` |

---

## Firestore Structure

```
crm/
  contact_select/          <- contact sheet selections
    items/
      {doc_id}             <- one doc per contact (from email_contacts)
        select: ""
        campaign: ""
        ...all contact fields

  crm_template/            <- CRM outreach pipeline
    items/
      {site_lead_id}       <- one doc per site
        ...all template columns

site_leads/
  {site_lead_id}
    crm_status: ""         <- written back from CRM template Status column
    crm_sales_person: ""   <- written back from Selger column
    crm_date: ""           <- written back from Dato lagt i column
```

---

## Workflows

### 1. Export contacts to contact sheet

Reads `email_contacts` from Firestore, skips Doc IDs already in sheet, appends new rows.
Sheet always has precedence for `Select` and `Campaign` columns.

```bash
python crm\contact_sync.py --countries NO
python crm\contact_sync.py --countries NO UK --status pending
python crm\contact_sync.py --countries NO --max 500
```

| Flag | Default | Description |
|---|---|---|
| `--countries` | all | Country codes e.g. `NO UK` |
| `--campaign` | — | Filter by campaign |
| `--status` | — | `pending` / `approved` / `sent` |
| `--max` | — | Cap new rows added |
| `--tab` | `contacts` | Sheet tab name |

### Sync contact sheet back to Firestore

Re-fetches fresh Firestore data, merges with sheet overrides (Select + Campaign win), writes back to sheet and Firestore.

```bash
python crm\contact_sync.py --sync-back
```

---

### 2. Push selected contacts to CRM template

Reads contact sheet, filters rows where `Select` is non-blank, groups by site, looks up `site_leads`, and appends new rows to the CRM template sheet + Firestore.

- One row per site (multiple contacts from same site are grouped)
- Skips sites already present in CRM template (matched by `site_lead_id`)
- After sheet write: upserts to `crm/crm_template/items`

```bash
python crm\contact_to_template.py --dry-run
python crm\contact_to_template.py
```

| Flag | Default | Description |
|---|---|---|
| `--contact-tab` | `contacts` | Contact sheet tab |
| `--template-tab` | `Outreach` | CRM template tab |
| `--dry-run` | — | Show what would be added without writing |

---

### 3. Sync CRM template sheet → Firestore

Reads the CRM template sheet and:
1. Upserts all rows to `crm/crm_template/items`
2. For each row with a `site_lead_id`, patches `site_leads/{id}` with:
   - `crm_status` ← Status column
   - `crm_sales_person` ← Selger column
   - `crm_date` ← Dato lagt i column

```bash
python crm\crm_template_sync.py
python crm\crm_template_sync.py --dry-run
```

### Enrich CRM template from site_leads

Matches each CRM template item to `site_leads` by website URL, merges site data (CRM values win), and writes `site_lead_id` back to the sheet.

```bash
python crm\crm_template_sync.py --enrich --dry-run
python crm\crm_template_sync.py --enrich
```

---

## CRM Template Columns

| # | Column | Source | Notes |
|---|---|---|---|
| 1 | Dato lagt i | today's date on insert | → `crm_date` in site_leads |
| 2 | Bedrift | `site_leads.company` or `domain` | |
| 3 | Nettside | `website` | |
| 4 | Bransje | `ai_sector \| ai_platform \| ai_company_type` | Combined |
| 5 | Størrelse | size label + location | `page_count` → Liten/Mellomstor/Stor/Enterprise/Ultra Enterprise |
| 6 | Oppsummert | `ai_summary` | |
| 7 | Land | `country` | |
| 8 | Site-sider | `page_count` | |
| 9 | Beslutningstaker | first contact `name` | |
| 10 | Rolle | first contact `title` | |
| 11 | E-post | first contact `email` | |
| 12 | Telefon | first contact `phone` | stored as text |
| 13 | Contacts | all contacts: `\|name,email,phone,title\|...` | |
| 14 | Score | — | manual |
| 15 | Status | — | manual → `crm_status` in site_leads |
| 16 | Selger | — | manual → `crm_sales_person` in site_leads |
| 17 | Kommentar | — | manual |
| 18 | Tilbud | — | manual |
| 19 | site_lead_id | normalized website URL | deduplication key |
| 20 | ai_sector | `site_leads.ai_sector` | raw |
| 21 | ai_company_type | `site_leads.ai_company_type` | raw |
| 22 | ai_platform | `site_leads.ai_platform` | raw |

### Størrelse mapping

| page_count | Label |
|---|---|
| < 500 | Liten |
| 500 – 1 999 | Mellomstor |
| 2 000 – 4 999 | Stor |
| 5 000 – 24 999 | Enterprise |
| ≥ 25 000 | Ultra Enterprise |

---

## Contact Sheet Columns

| # | Column | Field | Notes |
|---|---|---|---|
| 1 | Select | manual | Non-blank = selected for CRM template push |
| 2 | Campaign | `campaign` | Sheet has precedence on sync |
| 3 | Tier | `tier_label` | |
| 4 | Outreach | `outreach_priority` | Direct / Strong / Role/Dept / Admin/Generic |
| 5 | Status | `status` | |
| 6 | Email | `email` | |
| 7 | Website | `website` | |
| 8 | Name | `name` | |
| 9 | Title | `title` | |
| 10 | Phone | `phone` | stored as text |
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
| 27 | Lead ID Site | `lead_id_site` | used for site grouping |
| 28 | Lead ID Leads | `lead_id_leads` | |
| 29 | Created | `created_at` | date only |
| 30 | Doc ID | `doc_id` | deduplication key |
