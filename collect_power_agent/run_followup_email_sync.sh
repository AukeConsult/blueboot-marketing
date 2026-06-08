#!/bin/bash
# run_followup_email_sync.sh — sync email history into contact follow-up logs
#
# Usage:
#   ./run_followup_email_sync.sh                     # all campaigns, last 7 days
#   ./run_followup_email_sync.sh --days 30           # 30-day window
#   ./run_followup_email_sync.sh --campaign NO_jun   # one campaign only
#   ./run_followup_email_sync.sh --dry-run           # preview without writing
#   ./run_followup_email_sync.sh --list-campaigns    # list all campaign IDs

set -e
cd "$(dirname "$0")"

source .venv/bin/activate

# Default lookback window — override by passing --days N on the command line
DAYS=7

echo ""
echo "============================================================"
echo " FOLLOW-UP EMAIL SYNC  |  Last ${DAYS} days"
echo "============================================================"

python app/followup_email_sync.py --days "$DAYS" "$@"
