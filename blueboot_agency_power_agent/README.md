# BlueBoot Agency Power Agent — Multi-country Docker version

Local Docker lead-generation agent for finding web agencies, WordPress/WooCommerce providers, SEO agencies, digital agencies and communication agencies that may resell BlueSearch.

Now supports:

- Norway (`NO`)
- Sweden (`SE`)
- Denmark (`DK`)
- Germany (`DE`)
- United Kingdom (`UK`)

It creates:

- `output/agency_leads.xlsx`
- `output/agency_leads.csv`
- `output/agency_leads.json`

## What the agent does

1. Loads country-specific search queries from `config/queries_<COUNTRY>.txt`.
2. Searches using Google Custom Search if configured, otherwise uses Bing HTML fallback.
3. Filters candidate domains by country TLD: `.no`, `.se`, `.dk`, `.de`, `.co.uk`, `.uk`.
4. Crawls each website and selected internal pages.
5. Extracts emails, phone numbers, contact pages, LinkedIn company links and metadata.
6. Detects technologies such as WordPress, WooCommerce, Webflow, Shopify, HubSpot and more.
7. Classifies each lead: web agency, WordPress, SEO, communication, public sector, AI interest.
8. Scores reseller fit from 0–100.
9. Generates a suggested sales angle and outreach email draft for BlueSearch.
10. Exports to Excel/CSV/JSON.

## Run on Windows

Double-click:

```bat
run.bat
```

Or run:

```bash
docker compose up --build
```

## Run on Mac/Linux

```bash
chmod +x run.sh
./run.sh
```

## Configure countries

Copy the example env file:

```bash
cp .env.example .env
```

Edit `.env`:

```env
COUNTRIES=NO,SE,DK,DE,UK
MAX_RESULTS_PER_QUERY=25
MAX_PAGES_PER_SITE=8
REQUEST_DELAY_SECONDS=1.0
```

Run only Sweden and Denmark:

```env
COUNTRIES=SE,DK
```

Run all built-in countries:

```env
COUNTRIES=ALL
```

Or override directly:

```bash
docker compose run --rm blueboot-agency-agent python app/lead_agent.py --countries SE,DK --max-results 20 --max-pages 6
```

## Query files

Each country has its own query file:

```text
config/queries_NO.txt
config/queries_SE.txt
config/queries_DK.txt
config/queries_DE.txt
config/queries_UK.txt
```

You can add or remove searches there.

## Country settings

Language, phone region, TLDs, keyword categories and crawler path hints are in:

```text
config/countries.json
```

This is where you tune country-specific words such as `webbyrå`, `webbureau`, `webagentur`, `digital agency`, `kommunikasjonsbyrå`, etc.

## Results

After the run, open:

```text
output/agency_leads.xlsx
```

Important columns:

- `country`
- `country_name`
- `company`
- `website`
- `emails`
- `phones`
- `linkedin`
- `detected_tech`
- `categories`
- `reseller_score`
- `priority`
- `suggested_angle`
- `outreach_email`

## Optional Google Search API

The agent works without keys by using a Bing fallback. For better and more stable search, add Google Custom Search credentials to `.env`:

```env
GOOGLE_API_KEY=your_key
GOOGLE_CSE_ID=your_cse_id
```

## Recommended first run

Start small:

```bash
docker compose run --rm blueboot-agency-agent python app/lead_agent.py --countries NO --max-results 10 --max-pages 4 --delay 1.0
```

Then scale:

```bash
docker compose run --rm blueboot-agency-agent python app/lead_agent.py --countries ALL --max-results 50 --max-pages 10 --delay 1.5
```

## Important

Use reasonable rate limits and only collect public business contact information. This agent is designed for B2B lead research, not aggressive scraping or spam.
