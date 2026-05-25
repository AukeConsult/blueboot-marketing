#!/usr/bin/env bash
# Run the BlueBoot Agency Power Agent directly with Python.
# Usage: ./run.sh [lead_agent.py arguments]
#   e.g. ./run.sh --countries NO,SE --mode both --max-country 100
set -e
cd "$(dirname "$0")"
[ -f .env ] && export $(grep -v '^#' .env | xargs)
python app/lead_agent.py "$@"
