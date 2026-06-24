#!/usr/bin/env bash
set -e
source "$(dirname "$0")/.venv/bin/activate"
python app/campaign_scrape_emails.py --campaign copenhagen "$@"
