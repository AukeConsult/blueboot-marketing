#!/bin/bash
set -e

echo "=== CRM Firebase Function Deploy ==="
echo ""

echo "[1/3] Setting up venv..."
if [ ! -f "functions-crm/venv/bin/activate" ] && [ ! -f "functions-crm/venv/Scripts/activate" ]; then
    python -m venv functions-crm/venv
    echo "  venv created"
else
    echo "  venv already exists"
fi

echo "[2/3] Installing requirements..."
functions-crm/venv/bin/pip install -r functions-crm/requirements.txt -q 2>/dev/null || \
functions-crm/venv/Scripts/pip install -r functions-crm/requirements.txt -q

echo "[3/3] Deploying to Firebase..."
firebase deploy --only functions:crm

echo ""
echo "=== Deploy Complete ==="
echo ""
echo "Endpoints:"
echo "  Trigger: https://us-central1-blueboot-market.cloudfunctions.net/crmApi/api/crm/contact-sync?countries=NO"
echo "  Status:  https://us-central1-blueboot-market.cloudfunctions.net/crmApi/api/crm/status/JOB_ID"
echo "  Jobs:    https://us-central1-blueboot-market.cloudfunctions.net/crmApi/api/crm/jobs"
echo ""
