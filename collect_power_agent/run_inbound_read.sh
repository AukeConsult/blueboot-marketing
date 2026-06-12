#!/bin/bash
# run_inbound_read.sh - read inbound/sent mail into contact logs
#
# Usage:
#   ./run_inbound_read.sh                     # all campaigns, last 7 days
#   ./run_inbound_read.sh --days 30           # 30-day window
#   ./run_inbound_read.sh --campaigns NO_jun  # one campaign only
#   ./run_inbound_read.sh --dry-run           # preview without writing
#   ./run_inbound_read.sh --list-campaigns    # list all campaign IDs

set -e
cd "$(dirname "$0")"

source .venv/bin/activate

DAYS=7

echo ""
echo "============================================================"
echo " INBOUND MAIL READ  |  Last ${DAYS} days"
echo "============================================================"

python app/inbound_read.py --days "$DAYS" "$@"

