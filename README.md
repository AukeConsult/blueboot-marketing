# Blueboot CRM and Marketing

Automated lead discovery, enrichment and outreach pipeline for BlueSearch — finding and contacting potential customers and reseller partners.

---

#### Good instruments don't make artists. But artists exploiting new instruments can make great new art. 

(This code is mostly done with Claude as the programmer buddy)

## What it does

Two independent discovery pipelines find leads, enrich them with AI classification, and feed them into a CRM-style outreach workflow.

```
SITE PIPELINE                    LEAD PIPELINE
(end-user companies)             (web agencies / resellers)
        │                                │
  Discover via Bing + Brave        Discover via Bing + Brave
  Crawl sitemaps + contacts        + agency directories
  AI classify + enrich             AI classify + score 0–100
        │                                │
        └──────────┬────────────────────┘
                   ▼
          email_contacts (Firestore)
          unified contact store
                   │
          ┌────────▼────────┐
          │   CRM Pipeline  │  ← Google Sheets workflow
          │   select leads  │    track outreach status
          └────────┬────────┘
                   │
          ┌────────▼────────┐
          │  Excel Verify   │  ← approve / reject contacts
          └────────┬────────┘
                   │
          ┌────────▼────────┐
          │  Outreach Sender│  ← personalised email
          └─────────────────┘
```

---

## Quick Start

```bash
# Site pipeline — Norway
python app\site_agent.py --countries NO
python app\site_enrich_agent.py --countries NO
python app\site_contact_enrich.py --countries NO
python app\site_email_check.py --countries NO
python app\site_smart_export.py --countries NO --write-contacts --campaign NO_jun

# Lead pipeline — Norway
python app\lead_agent.py --countries NO --mode both
python app\lead_enrich_agent.py --countries NO
python app\lead_enrich_contacts.py --countries NO
python app\leads_email_check.py --countries NO
python app\leads_smart_export.py --countries NO --write-contacts --campaign NO_jun

# CRM workflow
python crm\contact_sync.py --countries NO --min-pages 500
# → fill Select column in Contact Sheet
python crm\push_and_sync.py
# → fill Status + Selger in CRM Template
python crm\template_sync.py
```

Or use the starter scripts:
```bash
run_site_pipeline.bat    # full site pipeline
run_lead_pipeline.bat    # full lead pipeline
```

---

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env     # fill in your API keys
```

Required keys in `.env`:

| Key | Purpose |
|---|---|
| `OPENAI_API_KEY` | AI classification and enrichment |
| `BRAVE_API_KEY` | Contact social profile enrichment |
| `FIREBASE_KEY_JSON` | Firestore database |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASSWORD` | Outreach email sending |

---

## CRM Dashboard

Live job monitoring and pipeline triggers:

**https://blueboot-market.web.app/**

Trigger operations and monitor async jobs from the browser. Built on Firebase Cloud Functions.

---

## Repository Structure

```
app/                      ← pipeline scripts
  site_agent.py           ← site discovery
  site_enrich_agent.py    ← AI site classification
  site_contact_enrich.py  ← contact enrichment
  site_email_check.py     ← email type classification
  site_smart_export.py    ← tiered Excel export
  lead_agent.py           ← agency discovery
  lead_enrich_agent.py    ← AI agency classification
  lead_enrich_contacts.py ← contact social profiles
  leads_email_check.py    ← email type classification
  leads_smart_export.py   ← tiered Excel export
  email_contacts_export.py← unified contact export
  gmail_outreach.py       ← outreach email sender
  campaign_exporter.py    ← export named campaigns
  maint_*.py              ← maintenance scripts

crm/                      ← CRM outreach workflow
  contact_sync.py         ← import contacts to Google Sheet
  push_and_sync.py        ← push selected → CRM template
  template_sync.py        ← sync CRM template → Firestore
  contact_to_template.py  ← push only (no sync back)
  crm_template_sync.py    ← sync + optional enrich
  config.py               ← sheet IDs
  setup_outreach_sheet.py ← one-time sheet setup

functions-crm/            ← Firebase Cloud Functions (CRM API)
  main.py                 ← REST API + Cloud Tasks worker
  crm/                    ← business logic (single source of truth)

public/                   ← CRM dashboard (Bootstrap)
  index.html

config/                   ← country queries, blocklists, catalogs
docs/                     ← Word documentation
exports/                  ← generated Excel files

setup_gcp.sh              ← one-time GCP setup
deploy_crm.sh             ← deploy Cloud Functions + hosting
```

---

## Documentation

| Document | Contents |
|---|---|
| [README.md](README.md) | Full technical reference — all scripts, CLI flags, Firestore structure |
| [crm/README.md](crm/README.md) | CRM pipeline — workflow, API, column reference |
| [docs/BlueBoot_Complete_Reference.docx](docs/BlueBoot_Complete_Reference.docx) | Combined Word document — architecture + full technical reference |
| [docs/email_contacts_field_reference.docx](docs/email_contacts_field_reference.docx) | email_contacts Firestore field reference |
| [.env.example](.env.example) | All required environment variables |

---

## Firestore Collections

| Collection | Written by | Contents |
|---|---|---|
| `site_leads` | site_agent | Crawled sites + AI classification |
| `site_leads/{id}/site_contacts` | site_agent | Extracted contacts per site |
| `leads` | lead_agent | Agency leads + reseller score |
| `leads/{id}/contacts` | lead_agent | Contacts per agency |
| `email_contacts` | smart_export scripts | Unified contacts (status=pending→sent) |
| `crm/contact_select/items` | contact_sync | Contact Sheet selections |
| `crm/crm_template/items` | push_and_sync, template_sync | CRM Template data |
| `crm_jobs` | API | Async job status |
| `sites_excluded` | site_agent | Rejected sites (never re-crawled) |

---

## API

The CRM pipeline is accessible as a REST API hosted on Firebase Cloud Run:

```
GET /api/crm/contact-sync?countries=NO&min_pages=500
GET /api/crm/push-and-sync
GET /api/crm/template-sync
GET /api/crm/status/{job_id}
GET /api/crm/jobs
```

Base URL: `https://us-central1-blueboot-market.cloudfunctions.net/crmApi`

---

## Supported Countries

NO · SE · DK · FI · UK · DE · FR · NL · BE · IE · ES · IT · PL · AT · IN · BR and more — see `config/countries.json`.

---

## Notes

- Collects public business contact information only
- Designed for B2B lead research, not aggressive scraping
- `blueboot_secrets.py` and `.env` are never committed
