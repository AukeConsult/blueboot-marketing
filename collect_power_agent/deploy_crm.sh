#!/bin/bash
set -e

echo "=== CRM Firebase Function Deploy ==="
echo ""

echo "[1/5] Generating functions-crm/.env from root .env..."
python sync_functions_env.py

echo "[2/5] Setting up venv..."
for FDIR in functions-crm; do
  if [ ! -f "$FDIR/venv/Scripts/activate" ] && [ ! -f "$FDIR/venv/bin/activate" ]; then
    echo "  creating $FDIR/venv..."
    python -m venv "$FDIR/venv"
  else
    echo "  $FDIR/venv exists"
  fi
done

echo "[3/5] Installing/updating requirements..."
for FDIR in functions-crm; do
  echo "  $FDIR..."
  "$FDIR/venv/Scripts/pip.exe" install -r "$FDIR/requirements.txt" -q 2>/dev/null || \
  "$FDIR/venv/bin/pip"         install -r "$FDIR/requirements.txt" -q
done

echo "[4/5] Deploying functions to Firebase..."
firebase deploy --only functions

echo "[5/5] Deploying hosting (CRM dashboard)..."
firebase deploy --only hosting

echo ""
echo "=== Deploy Complete ==="
echo ""
echo "Dashboard:"
echo "  https://blueboot-market.web.app/"
echo ""
