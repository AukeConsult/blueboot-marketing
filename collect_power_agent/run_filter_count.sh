#!/usr/bin/env bash
# Run / verify the filter-facets count job.
#   ./run_filter_count.sh --facet site_leads
#   ./run_filter_count.sh --facet NO_ecom --compare
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source .venv/bin/activate 2>/dev/null || source venv/bin/activate
python app/filter_count.py "$@"
