#!/bin/bash
# Test campaign API endpoints
# Usage: bash test_campaign_api.sh [campaign_id]
# Example: bash test_campaign_api.sh NO_jun

BASE="https://us-central1-blueboot-market.cloudfunctions.net/crmApi"
CAMPAIGN=${1:-"NO_jun"}

pass=0; fail=0

check() {
  local label=$1; local response=$2; local expect=$3
  if echo "$response" | python -c "import sys,json; d=json.load(sys.stdin); assert '$expect' in str(d), f'Missing: $expect'" 2>/dev/null; then
    echo "  PASS  $label"
    pass=$((pass+1))
  else
    echo "  FAIL  $label"
    echo "        Response: $(echo $response | python -m json.tool 2>/dev/null | head -5)"
    fail=$((fail+1))
  fi
}

echo ""
echo "=== Campaign API Tests (campaign: $CAMPAIGN) ==="
echo ""

# --- 1. List all campaigns ---
echo "[1] List all campaigns..."
R=$(curl -s "$BASE/api/crm/campaigns")
check "returns campaigns array" "$R" "campaigns"
check "returns count" "$R" "count"
echo "    $(echo $R | python -c "import sys,json; d=json.load(sys.stdin); print(f\"  {d.get('count',0)} campaigns found\")" 2>/dev/null)"
echo ""

# --- 2. List filtered by status ---
echo "[2] List campaigns with status=draft..."
R=$(curl -s "$BASE/api/crm/campaigns?status=draft")
check "returns campaigns array" "$R" "campaigns"
echo ""

# --- 3. Get single campaign ---
echo "[3] Get campaign '$CAMPAIGN'..."
R=$(curl -s "$BASE/api/crm/campaigns/$CAMPAIGN")
if echo "$R" | python -c "import sys,json; d=json.load(sys.stdin); assert 'campaign_id' in d" 2>/dev/null; then
  echo "  PASS  campaign found"
  echo "        $(echo $R | python -c "import sys,json; d=json.load(sys.stdin); print(f\"status={d.get('status','?')} contacts={d.get('contact_count','?')} sites={d.get('sites_count','?')}\")  " 2>/dev/null)"
  pass=$((pass+1))
else
  echo "  FAIL  campaign not found (run sync_campaign.py $CAMPAIGN first)"
  fail=$((fail+1))
fi
echo ""

# --- 4. Get non-existent campaign ---
echo "[4] Get non-existent campaign..."
R=$(curl -s "$BASE/api/crm/campaigns/campaign_does_not_exist_xyz")
check "returns 404 error" "$R" "not found"
echo ""

# --- 5. Update status to dosend ---
echo "[5] Update status to 'dosend'..."
R=$(curl -s -X POST "$BASE/api/crm/campaigns/$CAMPAIGN" \
  -H "Content-Type: application/json" \
  -d '{"status":"dosend"}')
check "update ok" "$R" "ok"
check "status is dosend" "$R" "dosend"
echo ""

# --- 6. Update mail subject + body ---
echo "[6] Update mail subject and body..."
R=$(curl -s -X POST "$BASE/api/crm/campaigns/$CAMPAIGN" \
  -H "Content-Type: application/json" \
  -d '{"mail":{"subject":"Test subject","body":"Hei {{name}}, test body"}}')
check "update ok" "$R" "ok"
check "mail subject updated" "$R" "Test subject"
echo ""

# --- 7. Update outreach email account ---
echo "[7] Update outreach email account..."
R=$(curl -s -X POST "$BASE/api/crm/campaigns/$CAMPAIGN" \
  -H "Content-Type: application/json" \
  -d '{"outreach_email_account":"test@blueboot.no"}')
check "update ok" "$R" "ok"
check "email account updated" "$R" "test@blueboot.no"
echo ""

# --- 8. Invalid status ---
echo "[8] Invalid status (should fail)..."
R=$(curl -s -X POST "$BASE/api/crm/campaigns/$CAMPAIGN" \
  -H "Content-Type: application/json" \
  -d '{"status":"invalid_status"}')
check "returns error" "$R" "error"
echo ""

# --- 9. Set status to sent (auto sets sent_at) ---
echo "[9] Set status to 'sent' (auto sent_at)..."
R=$(curl -s -X POST "$BASE/api/crm/campaigns/$CAMPAIGN" \
  -H "Content-Type: application/json" \
  -d '{"status":"sent"}')
check "update ok" "$R" "ok"
check "status is sent" "$R" "sent"
check "sent_at is set" "$R" "sent_at"
echo ""

# --- 10. Reset back to draft ---
echo "[10] Reset status back to draft..."
R=$(curl -s -X POST "$BASE/api/crm/campaigns/$CAMPAIGN" \
  -H "Content-Type: application/json" \
  -d '{"status":"draft"}')
check "reset ok" "$R" "ok"
echo ""

# --- 11. Verify final state ---
echo "[11] Verify final campaign state..."
R=$(curl -s "$BASE/api/crm/campaigns/$CAMPAIGN")
check "campaign_id present" "$R" "campaign_id"
check "mail present" "$R" "mail"
check "outreach_email_account present" "$R" "outreach_email_account"
echo "    Final state:"
echo "$R" | python -c "
import sys,json
d=json.load(sys.stdin)
print(f'  status={d.get(\"status\",\"?\")}, contacts={d.get(\"contact_count\",\"?\")}, sites={d.get(\"sites_count\",\"?\")}')
m=d.get('mail',{})
print(f'  mail.subject={m.get(\"subject\",\"?\")[:40]}')
print(f'  outreach_account={d.get(\"outreach_email_account\",\"?\")}')
" 2>/dev/null
echo ""

echo "=== Results: $pass passed, $fail failed ==="
echo ""

echo "=== Jobs API Tests ==="
echo ""

# --- 12. List jobs (default) ---
echo "[12] List jobs (default sort, limit 10)..."
R=$(curl -s "$BASE/api/crm/jobs?limit=10")
check "returns jobs array" "$R" "jobs"
check "returns count" "$R" "count"
echo "    $(echo $R | python -c "import sys,json; d=json.load(sys.stdin); print(f\"  {d.get('count',0)} jobs found\")" 2>/dev/null)"
echo ""

# --- 13. List running jobs only ---
echo "[13] List running jobs only (?running=true)..."
R=$(curl -s "$BASE/api/crm/jobs?running=true")
check "returns jobs array" "$R" "jobs"
echo "    $(echo $R | python -c "import sys,json; d=json.load(sys.stdin); jobs=d.get('jobs',[]); print(f\"  {len(jobs)} running/queued jobs\")" 2>/dev/null)"
echo ""

# --- 14. List running jobs for specific campaign ---
echo "[14] List running jobs for campaign '$CAMPAIGN'..."
R=$(curl -s "$BASE/api/crm/jobs?running=true&campaign_id=$CAMPAIGN")
check "returns jobs array" "$R" "jobs"
echo "    $(echo $R | python -c "import sys,json; d=json.load(sys.stdin); jobs=d.get('jobs',[]); print(f\"  {len(jobs)} running jobs for $CAMPAIGN\")" 2>/dev/null)"
echo ""

# --- 15. Trigger campaign-sync and check job appears ---
echo "[15] Trigger campaign-sync and verify job tracking..."
R=$(curl -s "$BASE/api/crm/campaign-sync?campaign_id=$CAMPAIGN")
check "job queued" "$R" "queued"
JOB=$(echo "$R" | python -c "import sys,json; print(json.load(sys.stdin).get('job_id',''))" 2>/dev/null)
echo "    job_id: $JOB"

if [ -n "$JOB" ]; then
    sleep 2
    echo "    Checking job appears in running list..."
    R2=$(curl -s "$BASE/api/crm/jobs?running=true&campaign_id=$CAMPAIGN")
    FOUND=$(echo "$R2" | python -c "import sys,json; d=json.load(sys.stdin); ids=[j['id'] for j in d.get('jobs',[])]; print('yes' if '$JOB' in ids else 'no')" 2>/dev/null)
    if [ "$FOUND" = "yes" ]; then
        echo "  PASS  job found in running list"
        pass=$((pass+1))
    else
        echo "  SKIP  job may have completed already"
    fi

    echo "    Polling status..."
    sleep 5
    R3=$(curl -s "$BASE/api/crm/status/$JOB")
    STATUS=$(echo "$R3" | python -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
    echo "    status after 5s: $STATUS"
    check "job has status field" "$R3" "status"
fi
echo ""

# --- 16. Jobs sorted descending ---
echo "[16] Verify jobs sorted by queued_at descending..."
R=$(curl -s "$BASE/api/crm/jobs?limit=5")
SORTED=$(echo "$R" | python -c "
import sys,json
d=json.load(sys.stdin)
jobs=d.get('jobs',[])
times=[j.get('queued_at','') for j in jobs if j.get('queued_at')]
is_sorted = times == sorted(times, reverse=True)
print('yes' if is_sorted else 'no')
" 2>/dev/null)
if [ "$SORTED" = "yes" ]; then
    echo "  PASS  jobs are sorted descending"
    pass=$((pass+1))
else
    echo "  FAIL  jobs not sorted descending"
    fail=$((fail+1))
fi
echo ""

echo "=== Results: $pass passed, $fail failed ==="
echo ""
