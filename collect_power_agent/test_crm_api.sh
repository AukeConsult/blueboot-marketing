#!/bin/bash

BASE="https://us-central1-blueboot-market.cloudfunctions.net/crmApi/api/crm"

echo "=== CRM API Tests ==="
echo ""

# -- whoami -------------------------------------------------------------------
echo "[1] whoami (check service account)..."
curl -s "$BASE/whoami" | python -m json.tool
echo ""

# -- contact-sync -------------------------------------------------------------
echo "[2] Triggering contact-sync (NO, max=10)..."
RESPONSE=$(curl -s "$BASE/contact-sync?countries=NO&max=10")
echo "$RESPONSE" | python -m json.tool
JOB_ID=$(echo "$RESPONSE" | python -c "import sys,json; print(json.load(sys.stdin).get('job_id',''))")
echo ""

if [ -n "$JOB_ID" ]; then
    echo "[2b] Polling status for job $JOB_ID (waiting 5s)..."
    sleep 5
    curl -s "$BASE/status/$JOB_ID" | python -m json.tool
    echo ""
fi

# -- push-and-sync ------------------------------------------------------------
echo "[3] Triggering push-and-sync..."
RESPONSE=$(curl -s "$BASE/push-and-sync")
echo "$RESPONSE" | python -m json.tool
JOB_ID2=$(echo "$RESPONSE" | python -c "import sys,json; print(json.load(sys.stdin).get('job_id',''))")
echo ""

if [ -n "$JOB_ID2" ]; then
    echo "[3b] Polling status for job $JOB_ID2 (waiting 5s)..."
    sleep 5
    curl -s "$BASE/status/$JOB_ID2" | python -m json.tool
    echo ""
fi

# -- template-sync ------------------------------------------------------------
echo "[4] Triggering template-sync..."
RESPONSE=$(curl -s "$BASE/template-sync")
echo "$RESPONSE" | python -m json.tool
JOB_ID3=$(echo "$RESPONSE" | python -c "import sys,json; print(json.load(sys.stdin).get('job_id',''))")
echo ""

if [ -n "$JOB_ID3" ]; then
    echo "[4b] Polling status for job $JOB_ID3 (waiting 5s)..."
    sleep 5
    curl -s "$BASE/status/$JOB_ID3" | python -m json.tool
    echo ""
fi

# -- list jobs ----------------------------------------------------------------
echo "[5] Listing recent jobs..."
curl -s "$BASE/jobs" | python -m json.tool
echo ""

echo "=== Done ==="
echo ""
echo "To poll a specific job manually:"
echo "  curl $BASE/status/JOB_ID"
