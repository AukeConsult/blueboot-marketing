#!/bin/bash
# deploy_all.sh — Deploy everything in one command:
#   1. CRM Firebase Cloud Function + Hosting  (deploy_crm.sh)
#   2. Batch runner image + Cloud Run         (deploy_batch.sh)
#
# Run from project root:  bash deploy_all.sh
#
# Skip a part if needed:
#   bash deploy_all.sh --skip-crm      (batch only)
#   bash deploy_all.sh --skip-batch    (CRM only)
set -euo pipefail

SKIP_CRM=false
SKIP_BATCH=false

for arg in "$@"; do
  case "$arg" in
    --skip-crm)   SKIP_CRM=true ;;
    --skip-batch) SKIP_BATCH=true ;;
    *) echo "Unknown argument: $arg" && exit 1 ;;
  esac
done

echo "========================================"
echo "  Full Deploy"
if $SKIP_CRM;   then echo "  (skipping CRM)"; fi
if $SKIP_BATCH; then echo "  (skipping Batch)"; fi
echo "========================================"
echo ""

if ! $SKIP_CRM; then
  echo ">>> [1/2] CRM — Firebase Function + Hosting"
  bash "$(dirname "$0")/deploy_crm.sh"
  echo ""
fi

if ! $SKIP_BATCH; then
  echo ">>> [2/2] Batch Runner — Cloud Build + Cloud Run"
  bash "$(dirname "$0")/deploy_batch.sh"
  echo ""
fi

echo "========================================"
echo "  All done!"
echo "  Dashboard: https://blueboot-market.web.app/"
echo "========================================"
