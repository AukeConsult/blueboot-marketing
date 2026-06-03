#!/bin/bash
set -e
echo "=== Deploying CRM hosting ==="
firebase deploy --only hosting
echo ""
echo "Dashboard: https://blueboot-market.web.app/"
