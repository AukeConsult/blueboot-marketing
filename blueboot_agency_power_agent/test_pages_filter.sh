#!/bin/bash
# Test min/max pages filter for contact-sync
# Run: bash test_pages_filter.sh

BASE="https://us-central1-blueboot-market.cloudfunctions.net/crmApi"

poll_job() {
  local job_id=$1
  local max_wait=60
  local waited=0
  echo "  polling job $job_id..."
  while [ $waited -lt $max_wait ]; do
    sleep 4
    waited=$((waited+4))
    result=$(curl -s "$BASE/api/crm/status/$job_id")
    status=$(echo "$result" | python -c "import sys,json; print(json.load(sys.stdin).get('status',''))")
    if [ "$status" = "done" ] || [ "$status" = "error" ]; then
      echo "$result" | python -m json.tool
      return
    fi
    echo "  still $status... (${waited}s)"
  done
  echo "  timed out after ${max_wait}s"
}

echo "=== Test 1: No page filter (baseline) ==="
R=$(curl -s "$BASE/api/crm/contact-sync?countries=NO&max=10")
echo "$R" | python -m json.tool
JOB=$(echo "$R" | python -c "import sys,json; print(json.load(sys.stdin).get('job_id',''))")
poll_job "$JOB"
echo ""

echo "=== Test 2: min_pages=1000 (medium+ sites only) ==="
R=$(curl -s "$BASE/api/crm/contact-sync?countries=NO&max=10&min_pages=1000")
echo "$R" | python -m json.tool
JOB=$(echo "$R" | python -c "import sys,json; print(json.load(sys.stdin).get('job_id',''))")
poll_job "$JOB"
echo ""

echo "=== Test 3: min_pages=5000 (enterprise only) ==="
R=$(curl -s "$BASE/api/crm/contact-sync?countries=NO&max=10&min_pages=5000")
echo "$R" | python -m json.tool
JOB=$(echo "$R" | python -c "import sys,json; print(json.load(sys.stdin).get('job_id',''))")
poll_job "$JOB"
echo ""

echo "=== Test 4: max_pages=500 (small sites only) ==="
R=$(curl -s "$BASE/api/crm/contact-sync?countries=NO&max=10&max_pages=500")
echo "$R" | python -m json.tool
JOB=$(echo "$R" | python -c "import sys,json; print(json.load(sys.stdin).get('job_id',''))")
poll_job "$JOB"
echo ""

echo "=== Test 5: range 500-5000 (mellomstor + stor) ==="
R=$(curl -s "$BASE/api/crm/contact-sync?countries=NO&max=10&min_pages=500&max_pages=5000")
echo "$R" | python -m json.tool
JOB=$(echo "$R" | python -c "import sys,json; print(json.load(sys.stdin).get('job_id',''))")
poll_job "$JOB"
echo ""

echo "=== Local CLI tests ==="
echo "Run these manually to verify locally:"
echo "  python crm\contact_sync.py --countries NO --max 5 --min-pages 1000"
echo "  python crm\contact_sync.py --countries NO --max 5 --max-pages 500"
echo "  python crm\contact_sync.py --countries NO --max 5 --min-pages 500 --max-pages 5000"
