#!/bin/bash
# run_outreach_send.sh - send campaign outreach mail (intro / follow-up)
#
# Default is a DRY RUN with body previews: nothing is sent or written.
# Real mail is sent only when --send is passed (you are asked to confirm).
#
# Usage:
#   ./run_outreach_send.sh --list-campaigns                      # list campaign IDs
#   ./run_outreach_send.sh --campaigns NO_jun --limit 5          # dry run + preview
#   ./run_outreach_send.sh --send --campaigns NO_jun --limit 5   # send 5 real mails
#   ./run_outreach_send.sh --send --mode both --campaigns NO_jun,SE_jun
#   ./run_outreach_send.sh --mode followup --campaigns NO_jun    # dry run follow-ups
#
# Options (passed through to app/outreach_send.py):
#   --mode intro|followup|both   default: intro
#   --campaigns ID [ID ...]      space/comma/semicolon/pipe separated; omit = all
#   --limit N                    max contacts per mode (default 500)
#   --send                       send real mail and write confirmations

set -e
cd "$(dirname "$0")"

source .venv/bin/activate

LIVE=0
LIST=0
for a in "$@"; do
  [ "$a" = "--send" ] && LIVE=1
  [ "$a" = "--list-campaigns" ] && LIST=1
done

echo ""
echo "============================================================"
if [ "$LIVE" = "1" ]; then
  echo " OUTREACH SEND  |  LIVE - real mail will be sent"
elif [ "$LIST" = "1" ]; then
  echo " OUTREACH SEND  |  campaign list"
else
  echo " OUTREACH SEND  |  DRY RUN - nothing is sent or written"
fi
echo "============================================================"

if [ "$LIVE" = "1" ]; then
  read -r -p "Type YES to send real mail: " CONFIRM
  if [ "$CONFIRM" != "YES" ]; then
    echo "Cancelled."
    exit 1
  fi
  python app/outreach_send.py "$@"
elif [ "$LIST" = "1" ]; then
  python app/outreach_send.py "$@"
else
  python app/outreach_send.py --preview "$@"
fi
