#!/bin/bash
set -e

echo "=== CRM Firebase Function Deploy ==="
echo ""

echo "[1/4] Setting up functions-crm venv..."
if [ ! -f "functions-crm/venv/Scripts/activate" ] && [ ! -f "functions-crm/venv/bin/activate" ]; then
    python -m venv functions-crm/venv
    echo "  venv created"
else
    echo "  venv exists, updating packages"
fi

echo "[2/4] Installing/updating requirements..."
functions-crm/venv/Scripts/pip.exe install -r functions-crm/requirements.txt -q 2>/dev/null || \
functions-crm/venv/bin/pip install -r functions-crm/requirements.txt -q

echo "[3/4] Deploying functions to Firebase..."
firebase deploy --only functions

echo "[4/4] Deploying hosting (CRM dashboard)..."
firebase deploy --only hosting

echo ""
echo "=== Deploy Complete ==="
echo ""
echo "API endpoints:"
echo "  Trigger: https://us-central1-blueboot-market.cloudfunctions.net/crmApi/api/crm/contact-sync?countries=NO"
echo "  Status:  https://us-central1-blueboot-market.cloudfunctions.net/crmApi/api/crm/status/JOB_ID"
echo "  Jobs:    https://us-central1-blueboot-market.cloudfunctions.net/crmApi/api/crm/jobs"
echo ""
echo "Dashboard:"
echo "  https://blueboot-market.web.app/"
echo ""