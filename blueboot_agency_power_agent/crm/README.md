# CRM

Google Sheets + Firestore CRM pipeline for outreach tracking.

---

## Architecture

Logic lives in **`functions-crm/crm/`** — single source of truth, deployed as a Firebase Cloud Function and called locally by CLI wrappers in `crm/`.

```
functions-crm/
  main.py                    <- Firebase Cloud Function (3 API endpoints)
  requirements.txt
  crm/
    contact_sync_lib.py      <- email_contacts -> contact sheet logic
    push_and_sync_lib.py     <- push selected -> CRM template + sync site_leads
    crm_template_sync_lib.py <- CRM template sheet -> Firestore + site_leads update
    sheets_config.py         <- shared sheet IDs and Firestore paths

crm/
  contact_sync.py            <- CLI wrapper for contact_sync_lib
  push_and_sync.py           <- CLI wrapper for push_and_sync_lib
  template_sync.py           <- CLI wrapper for crm_template_sync_lib
```

---

## Setup

1. Download OAuth2 client secret from GCP Console → APIs & Services → Credentials → OAuth 2.0 Client IDs (Desktop app)
2. Save as `config/google_oauth_client.json`
3. Enable Google Sheets API in GCP Console
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
  contact_select/
    items/ {doc_id}          <- contacts from email_contacts
      select, campaign, ...

  crm_template/
    items/ {site_lead_id}    <- one doc per site in CRM template
      ...all template columns

site_leads/ {site_lead_id}
  crm_status                 <- from Status column
  crm_sales_person           <- from Selger column
  crm_date                   <- from Dato lagt i column
```

---

## Workflow

```
1. contact-sync      fill contact sheet from email_contacts
2. (manual)          fill Select column in contact sheet
3. push-and-sync     push selected -> CRM template + sync to Firestore + update site_leads
4. (manual)          fill Status and Selger in CRM template
5. template-sync     sync CRM template -> Firestore + push crm_status/crm_sales_person back
```

---

## CLI Commands

### 1. contact-sync
Copies contacts from `email_contacts` (default: country=NO) to the contact sheet.
Skips contacts already in the sheet. Also upserts to `crm/contact_select/items`.

```bash
python crm\contact_sync.py --countries NO
python crm\contact_sync.py --countries NO UK --max 500
python crm\contact_sync.py --countries NO --status pending --campaign NO_jun
python crm\contact_sync.py --sync-back
```

| Flag | Default | Description |
|---|---|---|
| `--countries` | all | Country codes e.g. `NO UK` |
| `--max` | — | Cap new rows added |
| `--status` | — | Filter by status |
| `--campaign` | — | Filter by campaign |
| `--sync-back` | — | Re-fetch Firestore data, merge with sheet, write back |

### 2. push-and-sync
Reads contact sheet (Select != blank), pushes new sites to CRM template sheet,
upserts to `crm/crm_template/items`, syncs `crm_status`/`crm_sales_person`/`crm_date`
back to `site_leads`. All in one call.

```bash
python crm\push_and_sync.py
python crm\push_and_sync.py --dry-run
python crm\push_and_sync.py --contact-tab contacts --template-tab Outreach
```

| Flag | Default | Description |
|---|---|---|
| `--dry-run` | — | Show what would be pushed without writing |
| `--contact-tab` | `contacts` | Contact sheet tab |
| `--template-tab` | `Outreach` | CRM template tab |

### 3. template-sync
Syncs the CRM template sheet to `crm/crm_template/items` in Firestore,
and pushes `crm_status`, `crm_sales_person`, `crm_date` back to `site_leads`.
Use this after manually editing the CRM template (Status, Selger columns).

```bash
python crm\template_sync.py
python crm\template_sync.py --tab Outreach
```

---

## API Endpoints (Firebase Cloud Function)

Base URL: `https://us-central1-blueboot-market.cloudfunctions.net/crmApi`

### contact-sync
```bash
curl -X POST .../api/crm/contact-sync \
  -H "Content-Type: application/json" \
  -d '{"countries": ["NO"], "max": 500}'
```
Optional body fields: `countries`, `max`, `status`, `campaign`

### push-and-sync
```bash
curl -X POST .../api/crm/push-and-sync \
  -H "Content-Type: application/json" \
  -d '{}'
```

### template-sync
```bash
curl -X POST .../api/crm/template-sync \
  -H "Content-Type: application/json" \
  -d '{}'
```

### whoami (debug)
```bash
curl .../api/crm/whoami
```

---

## Deploy

```bash
# Create venv for Firebase deploy (one time)
cd functions-crm
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
deactivate
cd ..

# Deploy
firebase deploy --only functions:crm
```

Share both Google Sheets with: `blueboot-market@appspot.gserviceaccount.com`

---

## CRM Template Columns

| # | Column | Source | Notes |
|---|---|---|---|
| 1 | Dato lagt i | today | → `crm_date` in site_leads |
| 2 | Bedrift | `company` / `domain` | |
| 3 | Nettside | `website` | |
| 4 | Bransje | `ai_sector \| ai_platform \| ai_company_type` | |
| 5 | Størrelse | size label + location | page_count based |
| 6 | Oppsummert | `ai_summary` | |
| 7 | Land | `country` | |
| 8 | Site-sider | `page_count` | |
| 9 | Beslutningstaker | first contact name | |
| 10 | Rolle | first contact title | |
| 11 | E-post | first contact email | |
| 12 | Telefon | first contact phone | text format |
| 13 | Contacts | `\|name,email,phone,title\|...` | all selected contacts |
| 14 | Score | — | manual |
| 15 | Status | — | manual → `crm_status` |
| 16 | Selger | — | manual → `crm_sales_person` |
| 17 | Kommentar | — | manual |
| 18 | Tilbud | — | manual |
| 19 | site_lead_id | normalized website | deduplication key |
| 20 | ai_sector | `site_leads.ai_sector` | |
| 21 | ai_company_type | `site_leads.ai_company_type` | |
| 22 | ai_platform | `site_leads.ai_platform` | |

### Størrelse mapping

| page_count | Label |
|---|---|
| < 500 | Liten |
| 500 – 1 999 | Mellomstor |
| 2 000 – 4 999 | Stor |
| 5 000 – 24 999 | Enterprise |
| ≥ 25 000 | Ultra Enterprise |
