# BlueBoot Agency Power Agent — Docker version

This is a local Docker lead-generation agent for finding Norwegian web agencies, WordPress/WooCommerce providers, SEO agencies, and communication agencies that may resell BlueSearch.

It creates:

- `output/agency_leads.xlsx`
- `output/agency_leads.csv`
- `output/agency_leads.json`

## What the agent does

1. Runs search queries from `config/queries.txt`.
2. Finds `.no` agency domains.
3. Crawls each website and selected internal pages.
4. Extracts emails, phone numbers, contact pages, LinkedIn company links and metadata.
5. Detects technologies such as WordPress, WooCommerce, Webflow, Shopify, HubSpot and more.
6. Classifies each lead: web agency, WordPress, SEO, communication, public sector, AI interest.
7. Scores reseller fit from 0–100.
8. Generates a suggested sales angle and email draft for BlueSearch.
9. Exports to Excel/CSV/JSON.

## Requirements

- Docker Desktop installed and running.
- Internet connection.

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

## Results

After the run, open:

```text
output/agency_leads.xlsx
```

## Configuration

Edit:

```text
config/queries.txt
```

to add more searches, for example:

```text
site:.no "WordPress" "nettsider"
site:.no "WooCommerce" "Norge"
site:.no "kommunikasjonsbyrå" "nettside"
```

Edit `.env` to tune runtime:

```env
MAX_RESULTS_PER_QUERY=25
MAX_PAGES_PER_SITE=8
REQUEST_DELAY_SECONDS=1.0
```

## Optional Google Search API

The agent works without keys by using a Bing HTML fallback.
For better and more stable search, add Google Custom Search credentials to `.env`:

```env
GOOGLE_API_KEY=your_key
GOOGLE_CSE_ID=your_cse_id
```

## Recommended first run

Start smaller:

```bash
docker compose run --rm blueboot-agency-agent --output output --max-results 10 --max-pages 4 --delay 1.0
```

Then scale up:

```bash
docker compose run --rm blueboot-agency-agent --output output --max-results 50 --max-pages 10 --delay 1.5
```

## Important

Use reasonable rate limits and only collect public business contact information. This agent is designed for B2B lead research, not aggressive scraping.
