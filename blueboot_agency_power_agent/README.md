# BlueBoot Agency Power Agent

## Pipeline Overview

Two independent pipelines share the same Firestore project. The **Site Pipeline** (Section 1)
is the actively developed one. The **Lead Agent Pipeline** (Section 2) is the original and
remains fully operational.

---

### Site Pipeline (Section 1 — current)

Discovers content-heavy websites, measures them via sitemap, extracts and enriches contacts.

```
── Discover & collect ─────────────────────────────────────────────────────────

1. site_agent.py              Discover sites via Bing + Brave search
                              → site_leads + site_contacts in Firestore

2. site_enrich_agent.py       AI classification of each site_lead (GPT)
                              → sector, type, platform, hosting, keywords,
                                summary, ai_contacts, confidence

3. site_contact_enrich.py     Enrich site_contacts via Brave Search + GPT
                              → occupation, company, linkedin, twitter,
                                facebook, other_links

4. site_location_enrich.py    AI-infer company city/region for each site_lead
                              → location, location_full, location_city,
                                location_region, location_country,
                                location_confidence, location_source
                              Batches of 50 sites, 3 parallel OpenAI calls.
                              Filter by --location when exporting.

── Maintenance ────────────────────────────────────────────────────────────────

5. maint_site_excluded_recheck.py   Re-check sites_excluded — recover passing sites
6. maint_site_sitemap_backfill.py   Backfill sitemap data on existing site_leads

── Export ─────────────────────────────────────────────────────────────────────

7. site_email_check.py        AI classify each contact: email type + contact role
                              → email_type, contact_type, outreach_priority (1–4)

8. site_smart_export.py       Tiered Excel (6 tiers by page count + signals)
                              → exports/site_prospects_<cc>_<ts>.xlsx
                              → writes to email_contacts via --write-contacts

── Unified Outreach ──────────────────────────────────────────────────────────

9. email_contacts_export.py   Unified Excel from email_contacts collection
                              → combines site + leads pipeline contacts
                              → filter by status, campaign, pipeline mark
```

**Quick start — Norway**

```bat
python app\site_agent.py --countries NO
python app\site_enrich_agent.py --countries NO
python app\site_contact_enrich.py --countries NO
python app\site_location_enrich.py --countries NO
python app\site_email_check.py --countries NO
python app\site_smart_export.py --countries NO --write-contacts --campaign NO_jun02
python app\email_contacts_export.py --countries NO --campaign NO_jun02
```

---

### Lead Agent Pipeline (Section 2 — legacy)

Finds web agencies, WordPress/WooCommerce providers and digital agencies. Scores them
for reseller fit and exports to Excel + Firestore.

```
── Discover ───────────────────────────────────────────────────────────────────

1. lead_agent.py              Search (Bing + Brave + Google) + catalog scraping
                              → leads + contacts in Firestore
                                Modes: search | catalog | both | audit
                                Brave runs in parallel with Bing per query
                                (requires BRAVE_API_KEY in .env)

── Enrich ─────────────────────────────────────────────────────────────────────

2. lead_enrich_agent.py       AI classification of each lead (GPT)
                              → sector, specialisation, client_base,
                                reseller_potential, platform, summary,
                                confidence

3. lead_enrich_contacts.py         Social profile enrichment via Bing search
                              → linkedin_personal, twitter, facebook,
                                instagram, telegram, whatsapp per contact

4. leads_email_check.py       AI classify each contact: email type + contact role
                              → email_type, contact_type, outreach_priority (1–4)

── Export ─────────────────────────────────────────────────────────────────────

5. leads_smart_export.py      Tiered Excel (5 tiers by reseller score)
                              → exports/leads_prospects_<cc>_<ts>.xlsx
                              → writes to email_contacts via --write-contacts

── Unified Outreach ──────────────────────────────────────────────────────────

5. email_contacts_export.py   Unified Excel from email_contacts collection
                              → combines site + leads pipeline contacts
                              → filter by status, campaign, pipeline mark
```

**Quick start — Norway**

```bat
python app\lead_agent.py --countries NO --mode both --max-country 500
python app\lead_enrich_agent.py --countries NO
python app\lead_enrich_contacts.py --countries NO --skip-enriched
python app\leads_email_check.py --countries NO
python app\leads_smart_export.py --countries NO --write-contacts --campaign NO_jun02
python app\email_contacts_export.py --countries NO --campaign NO_jun02
```

---

---

## End-to-End Procedures

### Site Pipeline — Full Workflow (from discovery to mail-ready)

```
1. DISCOVER   site_agent.py              Search Bing + Brave, crawl sites, extract contacts
2. CLASSIFY   site_enrich_agent.py       GPT: sector, country, platform, hosting, summary
3. ENRICH     site_contact_enrich.py     Brave Search + GPT: occupation, LinkedIn, socials
4. LOCATE     site_location_enrich.py    GPT: city, region, full location text per site
5. CHECK      site_email_check.py        AI classify email type + contact role (priority 1–4)
6. EXPORT     site_smart_export.py       Tiered Excel + write to email_contacts
7. REVIEW     email_contacts_export.py   Unified Excel for review and approval
```

**Step-by-step (India example):**

```bat
call .venv\Scripts\activate.bat

REM 1. Discover sites (Bing + Brave search, crawl, extract contacts)
python app\site_agent.py --countries IN

REM 2. AI classify each site (GPT — needs OPENAI_API_KEY)
python app\site_enrich_agent.py --countries IN

REM 3. Enrich contacts (Brave Search + GPT — needs BRAVE_API_KEY + OPENAI_API_KEY)
python app\site_contact_enrich.py --countries IN

REM 4. Infer city + location for each site (dry-run 20 first to verify)
python app\site_location_enrich.py --countries IN --dry-run 20
python app\site_location_enrich.py --countries IN

REM 5. Classify email type and contact role (dry-run 20 to verify)
python app\site_email_check.py --countries IN --dry-run 20
python app\site_email_check.py --countries IN

REM 6. Export tiered Excel and write to email_contacts
python app\site_smart_export.py --countries IN --write-contacts --campaign IN_jun02

REM 7. Export unified review Excel
python app\email_contacts_export.py --countries IN --campaign IN_jun02 --status pending
```

**Filtering options at export time (Step 4):**

| Flag | Purpose |
|---|---|
| `--countries IN` | Filter by ai_country |
| `--min-pages N` | Minimum page count |
| `--outreach-priority N` | Only contacts with priority <= N (1=best) |
| `--write-contacts` | Write to email_contacts Firestore collection |
| `--campaign NAME` | Tag written to email_contacts docs |
| `--dry-run-contacts` | Preview write without committing |

**What gets created:**

```
site_leads/{lead_id}                          ← crawled site data + ai_* fields
site_leads/{lead_id}/site_contacts/{id}       ← scraped contacts
email_contacts/{doc_id}                       ← unified contact (mark_site_leads=true)
    status = pending
    approved = '' (filled in during review)
exports/site_prospects_IN_<ts>.xlsx           ← tiered Excel
exports/email_contacts_IN_<ts>.xlsx           ← unified review Excel
```

---

### Lead Agent Pipeline — Full Workflow (from discovery to mail-ready)

```
1. DISCOVER   lead_agent.py              Bing + Brave search, crawl agency sites
2. CLASSIFY   lead_enrich_agent.py       GPT: sector, specialisation, reseller fit
3. ENRICH     lead_enrich_contacts.py    Bing: LinkedIn, Twitter, social profiles
4. CHECK      leads_email_check.py       AI classify email type + contact role (priority 1–4)
5. EXPORT     leads_smart_export.py      Tiered Excel + write to email_contacts
6. REVIEW     email_contacts_export.py   Unified Excel for review and approval
```

**Step-by-step (UK example):**

```bat
call .venv\Scripts\activate.bat

REM 1. Discover agency leads (Bing + Brave + GitHub)
python app\lead_agent.py --countries UK --mode both

REM 2. AI classify leads (GPT — needs OPENAI_API_KEY)
python app\lead_enrich_agent.py --countries UK

REM 3. Enrich contacts with social profiles (Bing search)
python app\lead_enrich_contacts.py --countries UK --skip-enriched

REM 4. Classify email type and contact role
python app\leads_email_check.py --countries UK --dry-run 20
python app\leads_email_check.py --countries UK

REM 5. Export tiered Excel and write to email_contacts
python app\leads_smart_export.py --countries UK --write-contacts --campaign UK_jun02

REM 5. Export unified review Excel
python app\email_contacts_export.py --countries UK --campaign UK_jun02 --status pending
```

**Filtering options at extract time (Step 4):**

| Flag | Purpose |
|---|---|
| `--countries UK` | Filter by country |
| `--min-score N` | Minimum reseller score (0–100) |
| `--outreach-priority N` | Only contacts with priority <= N (1=best) |
| `--write-contacts` | Write to email_contacts Firestore collection |
| `--campaign NAME` | Tag written to email_contacts docs |
| `--dry-run-contacts` | Preview write without committing |

**What gets created:**

```
leads/{lead_id}                               ← crawled lead data + ai_* fields
leads/{lead_id}/contacts/{id}                 ← scraped contacts
email_contacts/{doc_id}                       ← unified contact (mark_leads=true)
    status = pending
    approved = '' (filled in during review)
exports/leads_prospects_UK_<ts>.xlsx          ← tiered Excel
exports/email_contacts_UK_<ts>.xlsx           ← unified review Excel
```

---

## Starter Scripts

Two ready-to-run batch files cover the full pipeline for each track. Edit the two variables at the top before running:

```bat
set COUNTRIES=NO        REM space-separated: NO SE DK
set CAMPAIGN=NO_jun02   REM tag written to email_contacts docs
```

| Script | Pipeline | Steps |
|---|---|---|
| `run_site_pipeline.bat` | Site | discover → classify → enrich contacts → locate → email check → export+write |
| `run_lead_pipeline.bat` | Lead | discover → classify → enrich contacts → email check → export+write |

Both scripts stop immediately if any step fails and print the review command at the end:
```bat
python app\email_contacts_export.py --countries NO --campaign NO_jun02 --status pending
```

---

## Section 1 — Site Agent Pipeline (current)

Async Python pipeline that discovers content-heavy websites via Bing search, measures site
size via sitemap, extracts contact emails, and stores results in Firestore.

### Architecture

```
Bing search (5 concurrent)
    ↓ URLs
Queue (asyncio)
    ↓
Site consumers (20 concurrent)
    ├─ Fetch robots.txt + sitemap → page count
    ├─ Fetch homepage → title, description, meta
    └─ Scrape contact pages → emails, phones
        ↓
Firestore
    site_leads/{lead_id}
    site_leads/{lead_id}/site_contacts/{contact_id}
    sites_excluded/{lead_id}   ← rejected sites, never re-fetched
```

### Scripts

| Script | Purpose |
|---|---|
| `app/site_agent.py` | Discovers sites, stores `site_leads` + `site_contacts` |
| `app/site_enrich_agent.py` | AI classification — sector, platform, hosting, contacts |
| `app/site_contact_enrich.py` | Enriches `site_contacts` via Brave Search + GPT |
| `app/site_email_check.py` | Classifies `site_contacts` by email type and contact role (OpenAI) |
| `app/site_smart_export.py` | Tiered Excel (6 tiers) + writes to `email_contacts` via `--write-contacts` |
| `site_scrape.bat` | Runs site_agent + site_enrich_agent for all countries |
| `run_site_pipeline.bat` | **Full Site Pipeline starter** — runs all 6 steps, edit `COUNTRIES` + `CAMPAIGN` at top |

### CLI — site_agent.py

```bash
python app/site_agent.py --countries NO,SE
python app/site_agent.py --countries NO --category real_estate
python app/site_agent.py --countries ALL --workers 20
python app/site_agent.py --countries NO --dry-run --max-results 20
python app/site_agent.py --countries NO --main-page-only
```

| Flag | Default | Description |
|---|---|---|
| `--countries` | `NO` | Space or comma-separated country codes e.g. `NO SE UK`|
| `--category` | _(all)_ | Run only one query category (e.g. `real_estate`, `tech`, `company`) |
| `--max-results` | `500` | Max Bing results per query |
| `--min-pages` | `0` | Minimum sitemap page count to keep a site |
| `--workers` | `20` | Parallel async workers |
| `--delay` | `1.5` | Seconds between Bing queries |
| `--no-firebase` | off | Skip all Firestore writes |
| `--dry-run` | off | Process sites but don't write to Firestore |
| `--collection` | `site_leads` | Firestore collection for accepted sites |
| `--excl-collection` | `sites_excluded` | Firestore collection for rejected sites |
| `--main-page-only` | off | Discard Bing results that are not homepage/root URLs |

### CLI — site_enrich_agent.py

```bash
python app/site_enrich_agent.py --countries NO,SE
python app/site_enrich_agent.py --countries NO,SE --limit 100
python app/site_enrich_agent.py --force          # re-classify already classified sites
```

Reads unprocessed `site_leads` documents and writes back AI-inferred fields:

| Field | Description |
|---|---|
| `ai_sector` | e.g. `public_sector`, `ecommerce`, `media`, `healthcare` |
| `ai_company_type` | e.g. `agency`, `inhouse`, `brand`, `institution` |
| `ai_country` | ISO 3166-1 alpha-2, inferred from TLD / language / address |
| `ai_keywords` | Up to 25 enriched English keywords |
| `ai_summary` | One-sentence description of the site |
| `ai_platform` | Detected CMS/site builder e.g. `WordPress`, `Shopify`, `Webflow` |
| `ai_hosting` | Detected hosting provider e.g. `WP Engine`, `Cloudflare`, `AWS` |
| `ai_contacts` | Array of `{name, email, role}` contacts found on the site |
| `ai_confidence` | Float 0.0–1.0 |
| `ai_classified_at` | ISO 8601 UTC timestamp |

### CLI — site_contact_enrich.py

```bash
python app/site_contact_enrich.py --countries NO,SE
python app/site_contact_enrich.py --countries NO --limit 100 --dry-run
python app/site_contact_enrich.py --force        # re-enrich already enriched contacts
python app/site_contact_enrich.py --concurrent 5
```

Reads every document from the `site_contacts` collectionGroup
(`site_leads/{lead_id}/site_contacts/{contact_id}`), runs a Brave Search per
contact, then uses GPT to extract and write back enriched fields:

| Field | Description |
|---|---|
| `occupation` | Confirmed/enriched job title |
| `company` | Confirmed company name |
| `linkedin` | LinkedIn profile URL |
| `twitter` | Twitter/X profile URL |
| `facebook` | Facebook profile URL |
| `other_links` | Array of other relevant URLs |
| `brave_enriched_at` | ISO 8601 UTC timestamp |

Requires `BRAVE_API_KEY` in `.env`. Skips contacts with no name, and skips
already-enriched contacts unless `--force` is passed.

### CLI — site_location_enrich.py

Enriches `site_leads` with AI-inferred city and country location. Sends batches of 50
sites to OpenAI (3 parallel), writing `location`, `location_full`, `location_city`,
`location_region`, `location_country`, `location_confidence`, and `location_source`.

```bash
# Dry-run 20 UK sites — prints inferred locations, no writes
python app/site_location_enrich.py --countries UK --dry-run 20

# Run for real
python app/site_location_enrich.py --countries UK
python app/site_location_enrich.py --countries IN

# Re-enrich sites already processed
python app/site_location_enrich.py --countries UK --force

# Larger batches, more parallelism
python app/site_location_enrich.py --countries IN --batch-size 50 --concurrent 4
```

| Flag | Default | Description |
|---|---|---|
| `--countries` | all | Space or comma-separated country codes e.g. `NO SE UK`|
| `--dry-run N` | off | Run on N sites, print results, skip Firestore writes |
| `--batch-size` | 50 | Sites per OpenAI call |
| `--concurrent` | 3 | Parallel OpenAI batch calls |
| `--force` | off | Re-enrich sites that already have `location_enriched_at` |
| `--limit N` | none | Max sites to process |

**Fields written to `site_leads`:**

| Field | Example |
|---|---|
| `location` | `London, England, United Kingdom` |
| `location_full` | `London, England, United Kingdom` |
| `location_city` | `London` |
| `location_region` | `England` |
| `location_country` | `UK` |
| `location_confidence` | `0.85` (1.0=address found, 0.3=TLD only) |
| `location_source` | `address` / `phone` / `postcode` / `content` / `company_name` / `domain` |

---

### CLI — site_email_check.py

Classifies each `site_contact` that has an email address. Sends batches of 50 to OpenAI
to determine the email type (personal vs role inbox) and the contact's likely role, then
writes `email_type`, `contact_type`, `outreach_priority`, and `email_checked_at` back to
the contact document.

```bat
REM Dry-run 20 UK contacts — print results, no writes
python app\site_email_check.py --countries UK --dry-run 20

REM Classify all UK contacts
python app\site_email_check.py --countries UK

REM Re-classify already-processed contacts
python app\site_email_check.py --countries UK --force
```

| Flag | Default | Description |
|---|---|---|
| `--countries` | all | Space or comma-separated country codes e.g. `NO SE UK`|
| `--dry-run N` | off | Run on N contacts, print results, skip writes |
| `--batch-size` | 50 | Contacts per OpenAI call |
| `--concurrent` | 3 | Parallel OpenAI batches |
| `--force` | off | Re-classify contacts already having `email_checked_at` |
| `--limit N` | none | Max contacts to process |

**Fields written to `site_contacts`:**

| Field | Values | Meaning |
|---|---|---|
| `email_type` | `personal` / `role` / `department` / `admin` | What kind of inbox it is |
| `contact_type` | `decision_maker` / `marketing` / `developer` / `sales` / `operations` / `unknown` | Likely role |
| `outreach_priority` | `1` – `4` | 1=personal email + decision maker/marketing (best); 4=admin/unknown (skip) |
| `email_checked_at` | ISO timestamp | When classified |

**Outreach priority logic:**

| Priority | Condition |
|---|---|
| 1 | Personal email + decision_maker or marketing role |
| 2 | Personal email + other role  OR  role/dept email + decision_maker |
| 3 | Role or department email + non-admin contact type |
| 4 | Admin email OR unknown type with generic inbox |

---

### CLI — site_smart_export.py

Tiered prospect export — reads `site_contacts` (collectionGroup) then batch-fetches parent
`site_leads`, scores each site into 6 tiers, and exports a colour-coded Excel.

```bat
python app\site_smart_export.py --countries UK
python app\site_smart_export.py --countries UK --outreach-priority 2
python app\site_smart_export.py --countries IN --min-pages 50 --out exports\india.xlsx
python app\site_smart_export.py --countries NO SE DK
```

| Flag | Default | Description |
|---|---|---|
| `--countries` | all | Space or comma-separated country codes e.g. `NO SE UK`|
| `--min-pages` | 0 | Minimum page count |
| `--outreach-priority N` | all | Only contacts with `outreach_priority` <= N |
| `--write-contacts` | off | Write contacts to `email_contacts` Firestore collection |
| `--campaign NAME` | — | Tag written to `email_contacts` (e.g. `UK_tier2_jun02`) |
| `--dry-run-contacts` | off | Print what would be written without writing |
| `--out` | `exports/site_prospects_<cc>_<ts>.xlsx` | Output path |

**Tier system (by page count + bonus signals):**

| Tier | Pages | Colour | Bonus signals can lift tiers 3–6 |
|---|---|---|---|
| 1 — Ultra Enterprise | >100,000 | Purple | Immune |
| 2 — Enterprise | >10,000 | Dark red | Immune |
| 3 — Hot | 500–10,000 | Red | WordPress + hot sector + 3 emails |
| 4 — Good | 100–500 | Orange | |
| 5 — Warm | 50–100 | Yellow | |
| 6 — Cold | <50 | Grey | |

**Excel sheets:** Contacts (colour-coded, sorted tier→pages), Summary (tier/sector/platform counts), WordPress vs Others.

---

---

## Section 2 — Lead Agent Pipeline (legacy)

Local Python lead-generation agent for finding web agencies, WordPress/WooCommerce providers, SEO agencies, digital agencies and communication agencies that may resell BlueSearch.

Supported countries: Norway (`NO`), Sweden (`SE`), Denmark (`DK`), Germany (`DE`), United Kingdom (`UK`), and any country with a `config/queries_<CODE>.txt` file.

### Scripts

| Script | Purpose |
|---|---|
| `app/lead_agent.py` | Discover agency leads via Bing + Brave + Google + catalog scraping |
| `app/lead_enrich_agent.py` | AI classification of each lead (GPT) → sector, specialisation, reseller_potential |
| `app/lead_enrich_contacts.py` | Enrich contacts with social media profiles via Bing |
| `app/leads_email_check.py` | AI classify each contact: email type + contact role + outreach priority |
| `app/leads_smart_export.py` | Tiered Excel (5 tiers by reseller score) + writes to `email_contacts` via `--write-contacts` |
| `app/email_contacts_export.py` | Unified review Excel from `email_contacts` — filter by status, campaign, mark |
| `run_lead_pipeline.bat` | **Full Lead Pipeline starter** — runs all 5 steps, edit `COUNTRIES` + `CAMPAIGN` at top |

---

## What the agent does

1. Loads country-specific search queries from `config/queries_<COUNTRY>.txt`.
2. Optionally scrapes curated agency directories (Clutch, Sortlist, DesignRush, GoodFirms, etc.).
3. Runs a GitHub organisation pre-pass to find agency orgs with a website.
4. Searches Bing + Brave (in parallel) and optionally Google Custom Search per query. Results are merged and de-duplicated. Brave requires `BRAVE_API_KEY` in `.env`; if not set it is silently skipped.
5. Filters candidate domains against a domain blocklist (`config/blocklist_domains.txt`).
6. Crawls each website and selected internal pages (contact, about, services, cases).
7. Extracts emails, phone numbers, contact pages, and LinkedIn company links.
8. Detects technologies: WordPress, WooCommerce, Webflow, Shopify, HubSpot, and more.
9. Classifies and scores each lead (0–100) for reseller fit.
10. Generates a suggested sales angle.
11. Writes results to Firestore in real time (one upsert per crawled site).
12. Exports to `output/agency_leads.xlsx`, `.csv`, and `.json`.

---

## Installation

```bash
pip install -r requirements.txt
```

Copy the example environment file and fill in your keys:

```bash
cp .env.example .env
```

---

## `lead_agent.py` — all parameters

| Parameter | Default | Description |
|---|---|---|
| `--mode` | `both` | `search` = keyword search only · `catalog` = directory scraping only · `both` = catalog then search · `audit` = TLD mismatch cleanup |
| `--countries` | all configured | Space or comma-separated country codes e.g. `NO SE UK`|
| `--queries` | _(per-country files)_ | Path to a custom queries file (overrides per-country files) |
| `--output` | `output` | Directory for Excel/CSV/JSON output files |
| `--max-results` | `200` | Max results per search engine per query (Bing, Brave, Google each) |
| `--min-score` | `50` | Minimum reseller score (0–100) to store a lead |
| `--max-pages` | `3` | Max pages to crawl per agency website |
| `--max-country` | `5000` | Stop a country once this many leads are found (0 = unlimited) |
| `--give-up-after` | `10` | Give up a country after this many consecutive empty queries |
| `--delay` | `1.0` | Seconds to wait between page fetches within one site |
| `--workers` | `20` | Parallel async workers |
| `--max-catalog-pages` | _(unlimited)_ | Limit pages per catalog source (useful for testing) |
| `--no-output` | off | Skip writing the Excel/CSV/JSON files |
| `--no-firebase` | off | Skip uploading results to Firestore |
| `--no-github` | off | Skip the GitHub org pre-pass |
| `--firebase-preload` | off | _(legacy flag, now always active)_ Preload seen domains from Firestore |
| `--firebase-collection` | `leads` | Override Firestore collection name |

### Audit mode (`--mode audit`)

Scans the entire `leads` collection and applies three cleanup passes. Always run with `--audit-dry-run` first.

```bat
python app\lead_agent.py --mode audit --audit-dry-run
python app\lead_agent.py --mode audit
```

| Pass | What it does |
|---|---|
| **Pass 1 — TLD corrections** | Leads with a ccTLD belonging to a different known country are re-assigned (`country` + `country_name` updated, original saved to `country_original`). Leads with a global TLD (`.com` / `.org` / `.net`, configurable in `countries.json` → `global_tlds`) are set to `country="*"` / `country_name="global"`. Leads with an unrecognised TLD not in `accepted_tlds` are deleted. |
| **Pass 2 — Contact audit** | Contacts with a blank or malformed email address are deleted. |
| **Pass 3 — Blocklist re-check** | Leads whose domain, website URL, company name, title, or description match the blocklist or content-negative keywords are deleted. |

### Discovery modes (`--mode`)

- **`search`** — runs Bing + Brave (+ optional Google CSE) queries in parallel per query, applies the full domain blocklist. Results are merged and de-duplicated before crawling.
- **`catalog`** — scrapes curated agency directories (Clutch, Sortlist, DesignRush, etc.); blocklist is **not** applied since catalog sources are already curated.
- **`both`** — catalog runs first, then search. Domains found in catalog phase are skipped during search.

### Example runs

```bat
REM Norway — both modes (catalog first then search), stop at 200 leads per country
python app\lead_agent.py --countries NO --mode both --max-country 200

REM Scandinavia — search only, 50 results per query
python app\lead_agent.py --countries NO SE DK --mode search --max-results 50

REM Catalog only — first 5 pages per source (quick test, skip GitHub pre-pass)
python app\lead_agent.py --mode catalog --max-catalog-pages 5 --no-github

REM Dry run — skip Excel output and Firestore upload
python app\lead_agent.py --countries NO --no-output --no-firebase

REM Audit mode — check for TLD mismatches in existing leads (dry run)
python app\lead_agent.py --mode audit --audit-dry-run

REM Re-crawl all sites ignoring existing Firestore history
python app\lead_agent.py --countries UK --mode both --force
```

---

## `lead_enrich_agent.py` — AI classification for leads collection

Reads documents from the `leads` collection and runs GPT classification to determine the agency type, specialisation, client base, and reseller potential. Uses the same batch/concurrent pattern as `site_enrich_agent.py`.

New fields written to each `leads/{id}` document:

| Field | Description |
|---|---|
| `ai_sector` | Agency category: `web_agency`, `seo_agency`, `design_agency`, `marketing_agency`, `hosting_provider`, `ecommerce_agency`, `communication_agency`, `it_consulting`, `pr_agency`, `media_agency`, `other` |
| `ai_specialisation` | Array of service tags, e.g. `["wordpress", "woocommerce", "seo"]` |
| `ai_client_base` | Primary client type: `SMB`, `enterprise`, `mixed`, `local`, `unknown` |
| `ai_reseller_potential` | Reseller fit: `high`, `medium`, `low` |
| `ai_platform` | Detected CMS/site builder |
| `ai_summary` | One-sentence agency description |
| `ai_confidence` | GPT confidence score (0.0–1.0) |
| `ai_classified_at` | ISO timestamp of classification run |

```bat
python app\lead_enrich_agent.py --countries NO
python app\lead_enrich_agent.py --countries NO,SE --force
python app\lead_enrich_agent.py --limit 200 --dry-run
```

### Parameters

| Parameter | Default | Description |
|---|---|---|
| `--collection NAME` | `leads` | Firestore collection to classify |
| `--countries CODES` | all | Comma-separated country codes, e.g. `NO,SE` |
| `--limit N` | _(none)_ | Maximum number of leads to classify |
| `--batch-size N` | `10` | Leads per GPT batch |
| `--concurrent N` | `3` | Parallel GPT batch workers |
| `--dry-run` | off | Print results without writing to Firestore |
| `--force` | off | Re-classify leads that already have `ai_classified_at` set |

## `lead_enrich_contacts.py` — social media profile enrichment

Reads contact documents from Firestore and adds personal social media profile links. Searches Bing in parallel for LinkedIn, Twitter/X, Facebook, Instagram and Telegram profiles; derives a WhatsApp deep-link from the contact's phone number without any search.

New fields written to each `contacts/{id}` document:

| Field | Source |
|---|---|
| `linkedin_personal` | Bing: `"Name" "Company" site:linkedin.com/in/` |
| `twitter` | Bing: `"Name" "Company" site:twitter.com OR site:x.com` |
| `facebook` | Bing: `"Name" "Company" site:facebook.com` |
| `instagram` | Bing: `"Name" "Company" site:instagram.com` |
| `telegram` | Bing: `"Name" "Company" site:t.me` |
| `whatsapp` | Derived from `phone` → `https://wa.me/{e164}` |
| `social_enriched_at` | ISO timestamp of enrichment run |

Only contacts with a valid email address and at least a name or phone number are processed. Already-populated fields are never overwritten.

```bat
cd app
python lead_enrich_contacts.py [options]
```

### Parameters

| Parameter | Default | Description |
|---|---|---|
| `--collection NAME` | `leads` | Firestore leads collection |
| `--countries CC [CC ...]` | all | Space or comma-separated country codes, e.g. `--countries NO SE UK` |
| `--limit N` | _(none)_ | Maximum number of contacts to process |
| `--workers N` | `50` | Parallel async workers (Bing searches run concurrently) |
| `--delay SECS` | `1.0` | Seconds to wait between Bing searches per worker |
| `--skip-enriched` | off | Skip contacts that already have `social_enriched_at` set |
| `--platforms LIST` | all | Comma-separated subset: `linkedin,twitter,facebook,instagram,telegram,whatsapp` |
| `--dry-run` | off | Print what would be written without touching Firestore |

### How parallelism works

Contacts are first filtered synchronously from Firestore. All filtered contacts are then enriched concurrently using `asyncio.gather` capped by a semaphore of `--workers`. Results are batch-written to Firestore after all workers finish. With 20 workers and 5 platforms per contact, throughput is roughly 20× faster than a sequential run.

### Example runs

```bat
REM Preview first — no writes
python app\lead_enrich_contacts.py --countries NO --limit 50 --dry-run

REM LinkedIn + WhatsApp only, Norway and Sweden, skip already enriched
python app\lead_enrich_contacts.py --countries NO SE --platforms linkedin,whatsapp --skip-enriched

REM Full run, all platforms
python app\lead_enrich_contacts.py --countries NO SE DK

REM Reduce concurrency if Bing starts rate-limiting
python app\lead_enrich_contacts.py --workers 10 --delay 2.0
```

---

### CLI — leads_email_check.py

Classifies `leads` contacts by email type and contact role. Mirrors `site_email_check.py` for the leads pipeline.

```bat
python app\leads_email_check.py --countries UK --dry-run 20
python app\leads_email_check.py --countries UK
python app\leads_email_check.py --countries NO SE DK
python app\leads_email_check.py --countries UK --force
```

| Flag | Default | Description |
|---|---|---|
| `--countries` | all | Space or comma-separated country codes |
| `--dry-run N` | off | Run on N contacts, print results, skip writes |
| `--force` | off | Re-classify contacts already checked |
| `--limit N` | none | Max contacts to process |
| `--batch-size N` | 50 | Contacts per OpenAI call |
| `--concurrent N` | 3 | Parallel OpenAI batch calls |

**Fields written to each contact:**

| Field | Values |
|---|---|
| `email_type` | `personal` / `role` / `department` / `admin` |
| `contact_type` | `decision_maker` / `marketing` / `developer` / `sales` / `operations` / `unknown` |
| `outreach_priority` | 1 (best) → 4 (lowest) |
| `email_checked_at` | ISO timestamp |

---

### CLI — leads_smart_export.py

Tiered reseller prospect export from `leads` + `contacts`.

| Flag | Default | Description |
|---|---|---|
| `--countries` | all | Space or comma-separated country codes e.g. `NO SE UK`|
| `--min-score N` | 0 | Minimum reseller score |
| `--outreach-priority N` | all | Only contacts with `outreach_priority` <= N |
| `--write-contacts` | off | Write contacts to `email_contacts` Firestore collection |
| `--campaign NAME` | — | Tag written to `email_contacts` (e.g. `UK_resellers_jun02`) |
| `--dry-run-contacts` | off | Print what would be written without writing |
| `--out` | `exports/leads_prospects_<cc>_<ts>.xlsx` | Output path |

---

---

## Unified Export — email_contacts

### CLI — email_contacts_export.py

Reads directly from the unified `email_contacts` collection. Use after one or both smart exports have run with `--write-contacts`.

```bat
python app\email_contacts_export.py --countries NO
python app\email_contacts_export.py --countries UK NO --status pending
python app\email_contacts_export.py --campaign NO_resellers_jun02
python app\email_contacts_export.py --mark both
python app\email_contacts_export.py --mark site --countries UK
```

| Flag | Default | Description |
|---|---|---|
| `--countries` | all | Space or comma-separated country codes e.g. `NO SE UK`|
| `--campaign NAME` | — | Filter by campaign tag |
| `--status` | all | `pending` / `approved` / `sent` / `replied` |
| `--mark` | all | `site` = SITE_LEADS only · `leads` = LEADS only · `both` = in both pipelines |
| `--out` | `exports/email_contacts_<cc>_<ts>.xlsx` | Output path |

**How `--mark` filtering works:**

| `--mark` | Firestore query | Client-side filter |
|---|---|---|
| `site` | `WHERE mark_site_leads == true` | — |
| `leads` | `WHERE mark_leads == true` | — |
| `both` | fetch all, filter in memory | drops docs where either mark is missing |

`--mark both` is client-side because Firestore requires a composite index to AND two boolean fields. For large collections, adding a `(mark_site_leads, mark_leads)` composite index would push this server-side.

**Excel sheets:** Contacts (all fields, sorted tier→company), Summary (tier/country/pipeline mark/status breakdowns), Sites (one row per unique domain).

**The `Approved` column (Contacts sheet, col 1):**
Always exported empty. Fill in `YES` to approve a contact for outreach. Blank = pending (untouched by import). Import script reads this column and sets `status=approved` in Firestore.

**The `Manual Campaign` column (Sites sheet, col 1):**
Always exported empty. Fill in a campaign name to tag a group of sites for targeted follow-up (e.g. `NO_healthcare_q3`, `UK_premium`). Allows sub-campaigns beyond the original `--campaign` filter.

For full column reference and review workflow see `docs/email_contacts_field_reference.docx`.

---

---

## Output files

After a run, the `output/` directory contains:

| File | Contents |
|---|---|
| `agency_leads.xlsx` | Leads sheet + Contacts sheet + Dashboard + Queries |
| `agency_leads.csv` | All leads, one row per lead |
| `agency_contacts.csv` | All contacts, one row per email address |
| `agency_leads.json` | All leads as JSON |
| `agency_contacts.json` | All contacts as JSON |
| `extract_leads_*.xlsx` | Filtered extracts produced by `maint_lead_extract.py` |
| `output/<campaign_id>/campaign.xlsx` | Campaign export (Summary + Leads + Contacts sheets) |
| `output/<campaign_id>/campaign.json` | Same campaign data as JSON |

### Key columns in leads

| Column | Description |
|---|---|
| `company` | Agency name (derived from domain) |
| `website` | Canonical website URL |
| `country` / `country_name` | ISO code and full name |
| `emails` | Comma-separated email addresses |
| `phones` | Comma-separated phone numbers |
| `linkedin` | LinkedIn company page URL |
| `detected_tech` | Technologies detected on the site |
| `categories` | Agency category labels |
| `reseller_score` | Fit score 0–100 |
| `priority` | A / B / C based on score |
| `reasons` | Scoring rationale |
| `suggested_angle` | Recommended BlueSearch sales angle |
| `found_by_search` | `yes` if discovered via keyword search |
| `found_by_catalog` | `yes` if discovered via directory catalog |
| `crawled_at` | ISO timestamp of last crawl |

---

## Configuration files

| File | Purpose |
|---|---|
| `config/countries.json` | Per-country settings: language, TLDs, keywords, phone region |
| `config/queries_<CODE>.txt` | Search queries per country |
| `config/blocklist_domains.txt` | Domain glob patterns and content negative keywords to exclude |
| `config/catalogs.json` | Directory catalog sources and page counts per country |

### `config/blocklist_domains.txt`

Single source of truth for all domain filtering. Contains two types of entries:

- **Domain glob patterns** (e.g. `*pizza*`, `*hotel*`) — any domain matching a pattern is skipped during search.
- **Content negative keywords** (under the `CONTENT NEGATIVE KEYWORDS` section) — plain substrings checked against visible page text during scoring; two or more occurrences trigger a score penalty.

Blocklist filtering applies to **search mode only**. Catalog sources are pre-curated and bypass the blocklist.

---

## Firebase / Firestore

Results are written to Firestore in real time as each site is crawled. Structure:

```
leads/{lead_id}                                                  — one document per agency
leads/{lead_id}/contacts/{id}                                    — one document per email address

leads_extract/{extract_name}                                     — one document per named extract
leads_extract/{extract_name}/leads_extracted/{lead_id}           — extracted lead snapshot
leads_extract/{extract_name}/leads_extracted/{lead_id}/
    contacts_extracted/{contact_id}                              — extracted contact snapshot
```

The `leads_extract` collection is populated by `maint_lead_extract.py --save-extract`. A lead can belong to at most one extract; duplicates are detected via a `collectionGroup` query on `leads_extracted` before each run.

Credentials are loaded from (in order):

1. `blueboot_secrets.py` in the project root (`fireBaseAdminKey` dict)
2. `FIREBASE_CREDENTIALS` environment variable (path to a service account JSON)
3. `config/serviceAccountKey.json`

At startup, already-crawled domains are preloaded from Firestore so they are never re-crawled within the same run or across runs.

---

## Optional Google Search API

The agent works without API keys using Bing as fallback. For more stable results, add Google Custom Search credentials to `.env`:

```env
GOOGLE_API_KEY=your_key
GOOGLE_CSE_ID=your_cse_id
```

---

## Notes

- Use reasonable rate limits (`--delay`, `--workers`) and only collect public business contact information.
- This agent is designed for B2B lead research, not aggressive scraping or spam.
- `blueboot_secrets.py` is never committed to version control.

---

---

---

## Maintenance Scripts

Scripts for one-off fixes, backfills, and legacy exports. All prefixed `maint_`.

### CLI — maint_site_excluded_recheck.py

```bash
python app/maint_site_excluded_recheck.py --countries NO
python app/maint_site_excluded_recheck.py --domains example.no
python app/maint_site_excluded_recheck.py --min-pages 50 --dry-run
```

Re-checks sites in `sites_excluded` that were previously rejected (e.g. due to
missing sitemaps). Sites that now pass are moved to `site_leads` and removed from
`sites_excluded`.

| Flag | Default | Description |
|---|---|---|
| `--countries` | all | Space or comma-separated country codes e.g. `NO SE UK`|
| `--domains` | all | Comma-separated domains to re-check |
| `--reason` | all | Only re-check sites whose exclusion reason contains this text |
| `--min-pages` | `50` | Minimum page count to recover a site |
| `--limit` | none | Max sites to re-check |
| `--concurrent` | `50` | Parallel fetches |
| `--dry-run` | off | Print results without writing to Firestore |
| `--force` | off | Re-check even sites with page_count > 0 |

### CLI — maint_site_sitemap_backfill.py

```bash
python app/maint_site_sitemap_backfill.py --countries NO
python app/maint_site_sitemap_backfill.py --countries NO --force
python app/maint_site_sitemap_backfill.py --limit 500 --dry-run
```

Backfills sitemap data (`page_count`, `sitemap_url`, `sitemap_type`, `sitemap_urls`,
`sitemap_oldest_date`) for existing `site_leads` documents that are missing it.

| Flag | Default | Description |
|---|---|---|
| `--countries` | all | Space or comma-separated country codes e.g. `NO SE UK`|
| `--limit` | none | Max leads to process |
| `--concurrent` | `20` | Parallel fetches |
| `--dry-run` | off | Print results without writing to Firestore |
| `--force` | off | Re-scan even leads that already have sitemap data |

### CLI — maint_site_leads_export.py

```bash
python app/maint_site_leads_export.py
python app/maint_site_leads_export.py --countries NO,SE
python app/maint_site_leads_export.py --countries NO --sector ecommerce
python app/maint_site_leads_export.py --countries NO --with-contacts-only
python app/maint_site_leads_export.py --out exports/no_leads.xlsx
```

Exports `site_leads` to Excel — one row per lead, with all contacts folded into a
single cell. Good for a full lead overview.

| Flag | Default | Description |
|---|---|---|
| `--countries` | all | Space or comma-separated country codes e.g. `NO SE UK`|
| `--sector` | all | Filter by `ai_sector` e.g. `ecommerce`, `technology` |
| `--category` | all | Filter by `query_category` e.g. `real_estate`, `healthcare` |
| `--location` | all | Keyword filter on `location_full` e.g. `London`, `Pune`, `Oslo` |
| `--with-contacts-only` | off | Only include leads that have at least one contact |
| `--limit` | none | Max leads to export |
| `--out` | auto-timestamped | Output `.xlsx` path |
| `--dry-run` | off | Count leads without fetching contacts or writing file |

Exports `site_contacts` to Excel — one row per contact, enriched with key fields from the parent `site_lead`. Country filtering uses **`ai_country`** from the site_lead only (the AI-detected country, which is more reliable than the scraped `country` field). Optionally saves the selection to a `site_campaigns` Firestore collection.

```bat
```

| Flag | Default | Description |
|---|---|---|
| `--countries` | all | Space or comma-separated country codes e.g. `NO SE UK`|
| `--sector` | all | Filter by `ai_sector` e.g. `ecommerce`, `technology` |
| `--category` | all | Filter by `query_category` e.g. `real_estate`, `healthcare` |
| `--location` | all | Keyword filter on `location_full` e.g. `London`, `Pune`, `Manchester` |
| `--with-email-only` | off | Only include contacts that have an email address |
| `--limit` | none | Max contacts to export |
| `--output` | auto-named | Output `.xlsx` path (default: `exports/site_contacts_<filter>_<date>.xlsx`) |
| `--campaign NAME` | off | Save filtered sites + contacts to `site_campaigns/<NAME>` in Firestore |
| `--page-count BUCKET` | all | Filter by site page count bucket (see table below) |
| `--force` | off | Re-assign sites already in another campaign (bypasses duplicate check) |

**Output sheets:**

| Sheet | Contents |
|---|---|
| `Contacts` | One row per contact — Doc ID, Site Doc ID, name, email, phone, title, occupation, company, linkedin, twitter, facebook, AI Country, then all site fields |
| `Summary` | Totals (contacts, with email/LinkedIn/phone, enriched), breakdown by AI Country and AI Sector |
| `Sites` | One row per site that has at least one contact in the selection — all site_lead fields |
| `Sites Summary` | Site totals, breakdown by AI Country and AI Sector |

**Page size buckets (`--page-count`):**

| Bucket | Page count range |
|---|---|
| `micro` | 1 – 50 |
| `small` | 51 – 500 |
| `medium` | 501 – 3 000 |
| `large` | 3 001 – 10 000 |
| `huge` | 10 001 – 100 000 |
| `ultra` | 100 001+ |
| `unknown` | 0 / None |

**`--campaign` Firestore structure:**

```
site_campaigns/{campaign}/
    campaign_id, created_at, site_count, contact_count
    filters: { countries, sector, category, with_email_only, limit }

    site_campaign_sites/{lead_id}/
        site_campaign_contacts/{contact_id}
```

**Duplicate prevention:** when `--campaign` is used, the script first queries the `site_campaign_sites` collectionGroup across all existing campaigns. Any site already claimed by another campaign is skipped and logged — it will not appear in two campaigns. Use `--force` to override and re-assign. The final output reports how many sites were saved vs skipped:

```
[campaign] SKIP agency-oslo.no            already in 'NO_may26'
[campaign] Done → 298 sites saved  14 skipped  1205 contacts
```

---

## `maint_lead_extract.py` — export a filtered extract from Firestore

Reads lead documents and their contacts sub-collections directly from Firestore and writes a focused Excel file. No local CSV is required. Global leads (`country="*"`) are excluded from all extracts.

```bat
python app\maint_lead_extract.py [options]
```

### Priority & Scoring

Every lead receives a `reseller_score` (0–100) and a `priority` label when it is crawled.
The score is built from keyword signals found on the agency's website:

| Signal | Points | What it detects |
|--------|--------|----------------|
| `web_agency` keyword | +25 | "web agency", "web design", "wordpress agency", "shopify developer", etc. |
| `wordpress` keyword | +25 | WordPress/WooCommerce/Elementor detected on site or in content |
| `care_plan` keyword | +15 | "care plan", "managed wordpress", "maintenance plan", "monthly retainer" |
| `seo` keyword | +18 | SEO services mentioned |
| `smb_focus` keyword | +12 | "small business", "local business", "SMB", "independent businesses" |
| `communication` keyword | +15 | PR/comms/social services |
| Agency language | up to +20 | "we build", "our portfolio", "get a quote", "our clients" |
| Services/clients/cases language | +8 | "services", "case studies", "portfolio" |
| Maintenance/support language | +6 | "maintenance", "hosting", "support", "SLA" |
| No `web_agency` keyword + no agency language | cap at 35 | Core-signal gate — prevents banks/telcos from scoring high |
| Negative keyword (≥2 occurrences) | −30 each, max −90 | Restaurant, salon, clinic, etc. |
| Adult content | −90 | Instant near-zero score |

**Priority bands:**

| Priority | Score | Meaning |
|----------|-------|---------|
| **A — High fit** | ≥ 75 | WordPress/WooCommerce agency with SMB clients, care plans, or maintenance retainers |
| **B — Good fit** | 55–74 | Digital/SEO agency with web capability, mixed client base |
| **C — Maybe** | 35–54 | Web-adjacent but unclear specialisation or client base |
| **D — Low fit** | < 35 | Enterprise-only, non-web sector, or insufficient signals |

A WordPress agency with SMB clients and a care plan will typically score 85–95 (A).

### AI Reseller Potential

After crawl scoring, `lead_enrich_agent.py` sends each lead to GPT for a second-pass
classification. This produces `ai_reseller_potential` — a qualitative judgement of how
likely the agency is to become a BlueSearch reseller partner.

| Value | Meaning |
|-------|---------|
| `high` | WordPress/WooCommerce agency with SMB/local clients, ongoing hosting or maintenance services. Ideal reseller target. |
| `medium` | Digital or SEO agency with web capability but unclear client base or mixed specialisation. Worth contacting. |
| `low` | Enterprise-only firm, pure brand/advertising agency, app-only developer, or unrelated sector. Skip or deprioritise. |

**Two scoring systems work together:**

- `reseller_score` + `priority` (A/B/C/D) — set at crawl time by keyword matching. Fast and deterministic.
- `ai_reseller_potential` — set by GPT after enrichment. Slower but reads actual site content for nuance.

Use both to build high-precision extracts:
```bat
python app\maint_lead_extract.py --countries UK --min-score 55 --priority A --priority B --ai-potential high --auto-name --save-extract
```
→ Extract ID: `UK_score55_a_b_high_jun02`

### Filter parameters

| Parameter | Default | Description |
|---|---|---|
| `--collection` | `leads` | Firestore collection name |
| `--output` | `<project_root>/output` | Directory to write the Excel file |
| `--min-score` | `0` | Minimum reseller_score to include |
| `--max-score` | `100` | Maximum reseller_score to include |
| `--countries CC [CC ...]` | all | Space or comma-separated country codes, e.g. `--countries NO SE UK` |
| `--source` | all | `search` / `catalog` / `both` — filter by discovery mode |
| `--query TEXT` | _(none)_ | Substring match on `source_query` (case-insensitive) |
| `--priority P` | all | Priority label(s), repeatable: `--priority A --priority B` |
| `--ai-potential LEVEL` | all | Filter by `ai_reseller_potential`: `high`, `medium`, `low` (repeatable: `--ai-potential high --ai-potential medium`) |
| `--with-email` | off | Only include leads with at least one contact email |
| `--keywords KW` | _(none)_ | Comma-separated keywords (OR logic). A lead matches if **any** keyword appears in `source_query`, `title`, `description`, `company`, `domain`, `website`, `keywords`, or `reasons`. E.g. `--keywords wordpress,woocommerce` |
| `--limit N` | _(none)_ | Maximum number of leads to include. Applied after all other filters. |
| `--out FILE` | auto-timestamped | Output filename |

### Save-extract parameters

Saving an extract persists the filtered leads to a dedicated `leads_extract` Firestore collection. A lead can only belong to **one** extract — any lead already in a previous extract is automatically skipped.

| Parameter | Default | Description |
|---|---|---|
| `--save-extract NAME` | _(none)_ | Save extract to `leads_extract/<NAME>` in Firestore |
| `--auto-name` | off | Auto-generate the extract ID from active filters. Overrides `--save-extract` name. Pattern: `{countries}_{score}_{priorities}_{ai_potentials}_{source}_{date}` e.g. `UK_score70_A_B_high_jun02` |
| `--extract-dry-run` | off | Preview what `--save-extract` would write without touching Firestore |

### Firestore structure written by `--save-extract`

```
leads_extract/
  {extract_name}/
    name, created_at, lead_count, contact_count, filters{…}
    leads_extracted/
      {lead_id}/
        (all lead fields)
        contacts_extracted/
          {contact_id}/   (all contact fields)
```

### Output Excel sheets

- **Extract** — one row per email contact with all lead fields merged in; leads without email appear at the bottom.
- **Leads** — one row per lead (raw Firestore fields).
- **Summary** — filter criteria, counts, and extract name.

### Example runs

```bat
REM A-priority Norwegian leads with email, score ≥ 70
python app\maint_lead_extract.py --min-score 70 --countries NO --priority A --with-email

REM Catalog-sourced leads across Norway and Sweden
python app\maint_lead_extract.py --source catalog --countries NO SE

REM All leads matching a specific query keyword
python app\maint_lead_extract.py --query "webbyrå"

REM Keyword search — WordPress or WooCommerce leads
python app\maint_lead_extract.py --keywords wordpress,woocommerce

REM Best leads — A/B priority + GPT high potential + email, auto-named extract
python app\maint_lead_extract.py --countries UK --min-score 55 --priority A --priority B --ai-potential high --with-email --auto-name --save-extract

REM High or medium AI potential from Norway and Sweden
python app\maint_lead_extract.py --countries NO SE --ai-potential high --ai-potential medium --priority A --auto-name --save-extract

REM India high-score WordPress agencies only
python app\maint_lead_extract.py --countries IN --min-score 70 --ai-potential high --keywords wordpress,woocommerce --auto-name --save-extract

REM Dry-run: preview what would be saved to Firestore
python app\maint_lead_extract.py ^
  --keywords wordpress ^
  --countries NO SE ^
  --min-score 60 ^
  --save-extract "wordpress_nordic_may26" ^
  --extract-dry-run

REM Live save — writes to leads_extract/wordpress_nordic_may26
python app\maint_lead_extract.py ^
  --keywords wordpress ^
  --countries NO SE ^
  --min-score 60 ^
  --save-extract "wordpress_nordic_may26"

REM Second extract — already-extracted leads are skipped automatically
python app\maint_lead_extract.py ^
  --keywords shopify ^
  --countries NO SE ^
  --save-extract "shopify_nordic_jun01"
```

### Function API

```python
from extract_leads import extract_leads

path = extract_leads(
    min_score=70,
    countries=["NO", "SE"],
    source="search",
    priorities=["A", "B"],
    with_email=True,
    keywords=["wordpress", "woocommerce"],
    save_extract="wordpress_nordic_may26",
    extract_dry_run=False,
    out_file="my_extract.xlsx",
)
```

---

## `maint_fix_contact_country.py` — one-time country field migration

Fixes an earlier data issue where contact documents stored the full country name (e.g. `"Norway"`) in the `country` field instead of the ISO code (`"NO"`). After this migration every contact document has:

- `country` — ISO code, e.g. `"NO"`
- `country_name` — full name, e.g. `"Norway"`

The script loads all lead documents into memory first (to get the correct `country` / `country_name` values), then streams all contacts via a `collectionGroup` query and batch-writes the corrected fields. Contacts whose fields are already correct are skipped.

```bat
REM Preview — no writes
python app\maint_fix_contact_country.py --dry-run

REM Live run
python app\maint_fix_contact_country.py
```

| Parameter | Default | Description |
|---|---|---|
| `--collection NAME` | `leads` | Firestore leads collection |
| `--dry-run` | off | Print what would be changed without writing |

This script only needs to be run once on existing data. All new contacts written by the crawler and catalog scraper now include both fields correctly.

---

---

## `campaign_exporter.py` — export a campaign to Excel + JSON

Reads a named campaign from the `leads_extract` Firestore collection (populated by `maint_lead_extract.py --save-extract`) and writes two files to `output/<campaign_id>/`:

| File | Contents |
|---|---|
| `campaign.xlsx` | Four sheets: Summary, Campaign, Leads, Contacts |
| `campaign.json` | Full campaign payload (schema_version, campaign, leads, contacts) |

```bat
:: List all saved campaigns
python app\campaign_exporter.py --list

:: Export a specific campaign
python app\campaign_exporter.py NO_high_score_may26

:: Export to a custom directory
python app\campaign_exporter.py NO_high_score_may26 --output exports\no_may26
```

### Parameters

| Parameter | Default | Description |
|---|---|---|
| `campaign_id` | _(required)_ | Firestore document ID under `leads_extract/` |
| `--list` | off | List all available campaign IDs and exit |
| `--output DIR` | `output/<campaign_id>/` | Custom output directory |

---

## `maint_statistics.py` — lead statistics & Firestore aggregations

Reads all leads and contacts from Firestore, computes aggregated statistics, writes results back to a `statistics` collection, and exports Excel reports to `output/`.

```bash
cd app
python maint_statistics.py [options]
```

Runs **both** aggregations by default. Use `--only` to target one.

### Parameters

| Parameter | Default | Description |
|---|---|---|
| `--leads-collection` | `leads` | Firestore collection to read leads from |
| `--stats-collection` | `statistics` | Firestore collection to write statistics into |
| `--output` | `output/` | Directory for Excel output files |
| `--only` | _(both)_ | `priority` or `reasons` — run only one aggregation |
| `--no-excel` | off | Skip writing Excel files |
| `--no-writeback` | off | Skip writing `reasons-list` back to each lead document |

**Collection overview** (`--only overview` or run by default) counts all major collections with breakdowns by country, ai_country, ai_sector, priority, reason, and **page size buckets** (micro/small/medium/large/huge/ultra).

### Aggregation 1 — Priority × Country

Counts leads and contacts per country, broken down by priority (A / B / C / unset).

Firestore structure written:

```
statistics/priority-pr-country               ← head document (grand totals + by_priority summary)
statistics/priority-pr-country/countries/NO  ← one sub-document per country
statistics/priority-pr-country/countries/SE
...
```

Head document fields: `generated_at`, `total_leads`, `total_contacts`, `country_codes`, `by_priority`.

Each country sub-document fields: `country`, `country_name`, `total_leads`, `total_contacts`, `by_priority`.

Excel output: `output/statistics.xlsx` — sheets **Summary**, **By Priority**, **By Country**, **Country x Prio**.

### Aggregation 2 — Reasons Count

Parses each lead's `reasons` field (`;`-separated signals, `:`-separated label/detail, `/`-separated compound labels) into individual reason tokens, counts occurrences per country, and optionally writes the parsed list back to each lead.

Delimiters applied in order:
- `;` — separates distinct reason groups
- `:` — strips detail, keeps label (`"wordpress: site, plugins"` → `"wordpress"`)
- `/` — expands compound labels (`"has services/customers/cases language"` → `"has services"`, `"customers"`, `"cases language"`)

Firestore structure written:

```
statistics/reasons-count               ← head document (global reason counts)
statistics/reasons-count/countries/NO  ← one sub-document per country
statistics/reasons-count/countries/SE
...
```

Reason counts are stored as a list of `{reason, count}` objects sorted by count descending.

Each lead document is also updated with a `reasons-list` field (array of parsed reason strings) unless `--no-writeback` is passed.

Excel output: `output/statistics_reasons.xlsx` — sheets **Global Reasons**, **By Country**.

### Example runs

```bash
# Run both aggregations (default)
python maint_statistics.py

# Priority stats only, no Excel
python maint_statistics.py --only priority --no-excel

# Reasons count only, skip writing back to leads
python maint_statistics.py --only reasons --no-writeback

# Write to a non-default stats collection
python maint_statistics.py --stats-collection statistics_test
```

### Function API

```python
from statistics import summarise_country_pr_priority, summarise_reasons_count
from statistics import export_to_excel, export_reasons_to_excel

# Priority aggregation
results = summarise_country_pr_priority(leads_collection="leads")
export_to_excel(results, outdir="output")

# Reasons aggregation (writeback on by default)
results = summarise_reasons_count(leads_collection="leads", writeback=True)
export_reasons_to_excel(results, outdir="output")
```

---

## `maint_firestore_index_sync.py` — manage Firestore composite indexes

Merges new composite indexes into `firestore.indexes.json`, de-duplicates against what is already defined, and optionally deploys them to Firestore. Also introspects the live Firestore database to report all top-level collections and their subcollections.

Run whenever you add a new collection or query pattern that needs a composite index.

```bat
:: Discover collections + merge indexes into firestore.indexes.json
python app\maint_firestore_index_sync.py

:: Preview merged result without writing
python app\maint_firestore_index_sync.py --dry-run

:: Merge and deploy to Firestore in one step
python app\maint_firestore_index_sync.py --deploy

:: Just list what collections/subcollections exist
python app\maint_firestore_index_sync.py --discover-only

:: Skip discovery, only merge the index file
python app\maint_firestore_index_sync.py --no-discover

:: Write to a custom path
python app\maint_firestore_index_sync.py --output config\firestore.indexes.json
```

### Parameters

| Parameter | Default | Description |
|---|---|---|
| `--output FILE` | `firestore.indexes.json` | Path to read/write the index file |
| `--dry-run` | off | Print merged JSON without writing the file |
| `--deploy` | off | Run `firebase deploy --only firestore:indexes` after writing |
| `--discover-only` | off | List collections and subcollections, then exit |
| `--no-discover` | off | Skip Firestore introspection, only merge the file |

### Indexes defined

Two collectionGroup scopes are managed — both work across all parent paths
(e.g. `site_leads` directly under `site_leads/` **and** nested under `site_campaigns/{id}/site_leads/`):

**`site_leads` collectionGroup**

| Fields | Use case |
|---|---|
| `ai_country` ↑ · `crawled_at` ↓ | Latest sites per country |
| `ai_country` ↑ · `ai_sector` ↑ · `crawled_at` ↓ | Sites by country + sector, newest first |
| `ai_country` ↑ · `ai_confidence` ↓ | Highest-confidence sites per country |
| `ai_country` ↑ · `ai_sector` ↑ · `ai_confidence` ↓ | Sector filter + confidence ranking |

**`site_contacts` collectionGroup**

| Fields | Use case |
|---|---|
| `ai_country` ↑ · `name` ↑ | All contacts for a country, alphabetical |
| `ai_country` ↑ · `email` ↑ · `brave_enriched_at` ↓ | Contacts with email, newest enriched first |
| `ai_country` ↑ · `occupation` ↑ · `name` ↑ | Filter by country + role |
| `ai_country` ↑ · `brave_enriched_at` ↓ | Contacts sorted by enrichment date |

### Deploy manually

```bat
firebase deploy --only firestore:indexes
```

---

## `gmail_outreach.py` — send outreach emails via Gmail

Sends personalised outreach emails to contacts from an Excel file, tracks opens and replies, and avoids re-sending to anyone who has already replied.

```bash
cd app
python gmail_outreach.py [options]
```

Requires Gmail OAuth credentials (`credentials.json` in the project root). On first run the browser opens for authorisation; a `token.json` is saved for future runs.

---

---

## Outreach Pipeline Architecture

The full system connects two discovery pipelines through a unified contact store to an automated email sender.

### Concept

```
SITE PIPELINE                  LEAD PIPELINE
(end-user companies)           (web agencies / resellers)
        │                              │
  discover → enrich             discover → enrich
  site_agent                    lead_agent
  site_enrich_agent             lead_enrich_agent
  site_contact_enrich           lead_enrich_contacts
  site_location_enrich          maint_leads_email_check
  site_email_check                     │
        │                              │
        └──────────┬───────────────────┘
                   ▼
          ┌─────────────────┐
          │  email_contacts │  ← Firestore collection (status=pending)
          │   (Firestore)   │     unified from both pipelines
          └────────┬────────┘
                   │
          ┌────────▼────────┐
          │  Excel Export   │  ← human reviews, approves / rejects
          │  + Import Back  │    status updated to approved
          └────────┬────────┘
                   │  status=approved only
          ┌────────▼────────┐
          │  Automated      │  ← personalised mail, rate-limited
          │  Outreach Sender│    tracks replies, updates status=sent
          └─────────────────┘
```

### Contact Status Lifecycle

| Status | Set by | Meaning |
|--------|--------|---------|
| `pending` | Pipeline export | Ready for human review |
| `approved` | Human via Excel import | OK to send |
| `rejected` | Human via Excel import | Never send |
| `sent` | Outreach sender | Mail dispatched |
| `replied` | Reply tracker | Contact responded |
| `bounced` | Send delivery | Invalid email |
| `unsubscribed` | Opt-out | Remove from all lists |
| `converted` | Manual / CRM | Became customer/partner |

### email_contacts Fields

| Field | Site | Leads | Description |
|-------|------|-------|-------------|
| `email` | ✅ | ✅ | Validated, clean email address |
| `name` | ✅ | ✅ | Contact name (cleaned via `clean_str`) |
| `title` | ✅ | ✅ | Job title (cleaned via `clean_str`) |
| `phone` | ✅ | ✅ | Phone number |
| `linkedin` | ✅ | ✅ | LinkedIn URL |
| `domain` | ✅ | ✅ | Company domain |
| `website` | ✅ | ✅ | Company website URL |
| `company` | ✅ | ✅ | Company name |
| `country` | ✅ | ✅ | Normalised country code via `resolve_country()` |
| `location` / `location_city` / `location_region` | ✅ | — | Location (site pipeline only) |
| `ai_sector` | ✅ | ✅ | GPT-classified sector |
| `ai_platform` | ✅ | ✅ | Platform (WordPress, Shopify, etc.) |
| `ai_summary` | ✅ | ✅ | AI-generated company summary |
| `ai_company_type` | ✅ | — | Company type (site pipeline only) |
| `ai_confidence` | ✅ | — | GPT confidence score (site pipeline only) |
| `ai_potential` | — | ✅ | Reseller potential: high/medium/low |
| `ai_client_base` | — | ✅ | SMB / enterprise / mixed |
| `reseller_score` | — | ✅ | Numeric reseller score 0–100 |
| `keywords` | ✅ | — | Extracted site keywords |
| `page_count` | ✅ | — | Site page count |
| `tier` / `tier_label` | ✅ | ✅ | Scoring tier (1–5/6) |
| `email_type` | ✅ | ✅ | `personal` / `role` / `department` / `admin` |
| `contact_type` | ✅ | ✅ | `decision_maker` / `marketing` / `developer` / etc. |
| `outreach_priority` | ✅ | ✅ | 1 (best) → 4 (weakest) |
| `mark_site_leads` | ✅ | — | `true` if written by site pipeline |
| `mark_leads` | — | ✅ | `true` if written by leads pipeline |
| `category_site` | ✅ | — | Search query category (site pipeline) |
| `category_leads` | — | ✅ | `catalog` or `search` (leads pipeline) |
| `doc_id` | ✅ | ✅ | Normalised Firestore document ID (from email) |
| `lead_id_site` | ✅ | — | Source `site_leads` document ID |
| `lead_id_leads` | — | ✅ | Source `leads` document ID |
| `contact_id` | ✅ | — | Source `site_contacts` document ID |
| `campaign` | ✅ | ✅ | Campaign tag (set via `--campaign`) |
| `personalisation` | ✅ | ✅ | `{name, full_name}` for mail merge |
| `status` | ✅ | ✅ | `pending` / `approved` / `rejected` / `sent` / `replied` / etc. |
| `created_at` | ✅ | ✅ | ISO timestamp — set once on first write, never updated |

### Architecture PDF

A full architecture document is saved at `docs/BlueSearch_Outreach_Pipeline.pdf`.
It covers all five stages, every script, the contact schema, and the status lifecycle.


---

## `config/catalogs.json` — Directory Catalog Sources

Defines all agency directory sources scraped in `--mode catalog` and `--mode both`. Contains **952 entries** across **23 countries**.

### Entry fields

| Field | Description |
|---|---|
| `name` | Human-readable label for the source |
| `type` | Scraper type — determines which scraper class handles it (see table below) |
| `url` | URL template — `{page}` is replaced with the page number at runtime |
| `pages` | Max pages to scrape. DesignRush: set high (50+), scraper stops on 404. Sortlist: always 1 (JS infinite-scroll). |
| `__platform_added` | Annotation only — marks entries added as WordPress/WooCommerce platform sources |
| `__new_dir` | Annotation only — marks entries added in a later expansion pass |

### Scraper types

| Type | Directory | Notes |
|---|---|---|
| `sortlist` | sortlist.com | JS infinite-scroll — `pages=1` scrapes ~20 top agencies only. Nordic countries use no URL prefix; UK/FR/DE use `/s/`; ES uses `/i/` for web-design, `/s/` for web-development. |
| `designrush` | designrush.com | Supports `?page=N` pagination. Set `pages` high (e.g. 50) — scraper stops automatically on 404. |
| `topdevelopers` | topdevelopers.co | Standard pagination with `?page=N`. |
| `dan` | digitalagencynetwork.com | Single-page listings per country. `pages=1`. |
| `proff` | proff.no / proff.se / proff.dk | Norwegian/Scandinavian business registry. Category + pagination in URL path. |
| `gulesider` | gulesider.no | Norwegian yellow pages. Category + pagination in URL path. |
| `pagesjaunes` | pagesjaunes.fr | French yellow pages. Search query + pagination in URL params. |
| `paginasamarillas` | paginasamarillas.es | Spanish yellow pages. Category + pagination in URL path. |
| `generic` | Various (Sitecore, Atlassian, HubSpot partner pages, etc.) | Catch-all scraper for partner directories. |

### Known issues and WAF blocks

| Directory | Status | Reason |
|---|---|---|
| **Clutch** | ❌ Removed | Cloudflare WAF returns 403 on all requests |
| **GoodFirms** | ❌ Removed | WAF drops connection after ~20s timeout |
| **Sortlist** | ⚠️ Limited | JS-rendered infinite scroll — only first ~20 agencies per category reachable |

### Supported countries (23)

| Code | Country | Primary sources |
|---|---|---|
| NO | Norway | sortlist, designrush, topdevelopers, dan, proff, gulesider |
| SE | Sweden | sortlist, designrush, topdevelopers, dan, proff |
| DK | Denmark | sortlist, designrush, topdevelopers, dan, proff |
| FI | Finland | sortlist, designrush, topdevelopers, dan |
| UK | United Kingdom | sortlist, designrush, topdevelopers, dan, generic (partner pages) |
| DE | Germany | sortlist, designrush, topdevelopers, dan, generic |
| FR | France | sortlist, designrush, topdevelopers, dan, pagesjaunes |
| NL | Netherlands | sortlist, designrush, topdevelopers, dan |
| BE | Belgium | sortlist, designrush, topdevelopers, dan |
| IE | Ireland | sortlist, designrush, topdevelopers, dan |
| ES | Spain | sortlist, designrush, topdevelopers, dan, paginasamarillas |
| IT | Italy | sortlist, designrush, topdevelopers, dan |
| PL | Poland | sortlist, designrush, topdevelopers, dan |
| AT | Austria | sortlist, designrush, topdevelopers, dan |
| IN | India | sortlist, designrush, topdevelopers, dan |
| BR | Brazil | sortlist, designrush, topdevelopers, dan |
| AR | Argentina | sortlist, designrush, topdevelopers |
| HU | Hungary | sortlist, designrush, topdevelopers |
| EE | Estonia | sortlist, designrush |
| LV | Latvia | sortlist, designrush |
| LT | Lithuania | sortlist, designrush |
| TH | Thailand | designrush |
| TN | Tunisia | sortlist, designrush |

### Adding a new catalog source

Add an entry to the relevant country array in `config/catalogs.json`:

```json
{
  "name": "MyDirectory UK – web design",
  "type": "designrush",
  "url": "https://www.mydirectory.com/web-design/uk?page={page}",
  "pages": 20
}
```

Use `type: "generic"` for partner pages that list agency websites directly (no pagination needed, set `pages: 1`). Run with `--mode catalog --max-catalog-pages 2` to test before committing.

---

## `config/site_agent_queries.json` — Site Agent Search Configuration

Drives `site_agent.py` — defines per-country search instructions for finding content-heavy websites that could benefit from BlueSearch AI search. Contains **1,996 queries** across **11 countries** and **37 categories**.

> **Goal:** Find sites with lots of pages and content where visitors need to search and find information — municipalities, public sector, e-commerce, healthcare, media, education, finance, etc.

### Country entry fields

| Field | Description |
|---|---|
| `name` | Full country name |
| `language` | Browser `Accept-Language` language code used in Bing search headers |
| `accept_language` | Full `Accept-Language` header value sent with requests |
| `description` | Human description of what kinds of sites to target in this country |
| `min_pages` | Minimum page count for a site to be stored — lower = more inclusive |
| `target_types` | List of site types the queries aim to find (used for scoring and keywords) |
| `query_categories` | Map of `category → [query strings]` — the actual Bing search queries |

### Per-country summary

| Country | `min_pages` | Language | Categories | Queries |
|---|---|---|---|---|
| NO — Norway | 50 | no | 16 | 208 |
| SE — Sweden | 50 | sv | 16 | 205 |
| DK — Denmark | 50 | da | 16 | 200 |
| FI — Finland | 50 | fi | 16 | 200 |
| UK — United Kingdom | 100 | en | 16 | 204 |
| DE — Germany | 50 | de | 16 | 203 |
| FR — France | 20 | fr | 19 | 224 |
| NL — Netherlands | 20 | nl | 19 | 222 |
| BE — Belgium | 15 | fr,nl | 5 | 32 |
| IN — India | 10 | en | 12 | 106 |
| EU — European Union | 4 | en | 16 | 192 |

`min_pages` reflects how content-heavy sites tend to be per market — UK/Nordic public sector sites are large, India and Belgium thresholds are lower to capture more results.

### Query categories (37 total)

| Category | Description |
|---|---|
| `municipality` | Local government, kommune, council websites |
| `public` / `public_sector` | Government agencies, public bodies, national institutions |
| `healthcare` | Hospitals, health trusts, clinic networks, patient information sites |
| `education` | Universities, schools, educational institutions |
| `media` | News sites, broadcasters, online publications |
| `company` | General businesses, manufacturers, B2B companies |
| `ecommerce` / `shop` | Online shops, retailers |
| `technology` / `tech` | SaaS, IT companies, tech platforms |
| `finance` | Banks, insurance, financial services |
| `real_estate` | Property portals, housing associations |
| `legal` | Law firms, legal information sites |
| `logistics` | Shipping, transport, logistics operators |
| `construction` | Building, infrastructure, engineering |
| `hospitality` | Hotels, tourism, restaurants |
| `hr` | HR platforms, recruitment, staffing |
| `association` | Industry associations, NGOs, member organisations |
| `company_cities` | City-specific company queries (NO/SE/DK) |
| `company_fr` / `company_nl` / `company_paris` | Country-specific company variants |
| `ecommerce_fr` / `ecommerce_nl` / `ecommerce_be` | Country-specific ecommerce variants |
| `saas_tech_fr` / `saas_tech_nl` / `tech_be` | Country-specific tech variants |
| `pune` / `mumbai` / `delhi` / `bangalore` / `chennai` / `hyderabad` / `ahmedabad` | India city-specific queries |
| `other_cities` | Non-capital city queries |

### How queries are used

`site_agent.py` iterates query categories round-robin across all countries. Each query string is sent to Bing search, results are deduplicated against already-seen domains, and new sites are crawled for sitemap + contacts.

Filter by category at export time:
```bat
python app\site_smart_export.py --countries NO --category municipality
python app\site_smart_export.py --countries NO --category ecommerce
```

Run only a specific category during discovery:
```bat
python app\site_agent.py --countries NO --category real_estate
```

### Adding new queries

Add entries to the `query_categories` dict for the relevant country:
```json
"healthcare": [
  "norsk helseforetak pasientinformasjon nettside",
  "sykehus norsk pasientportal"
]
```

Or add a new category entirely — it will be picked up automatically in the round-robin. Keep queries in the target language and include site-structure terms (`nettside`, `portal`, `informasjon`) to bias toward large content sites rather than landing pages.
