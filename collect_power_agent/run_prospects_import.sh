#!/bin/bash
# run_prospects_import.sh -- sync the prospect catalogue into campaigns (dry run unless --apply)
cd "$(dirname "$0")" || exit 1
PY=python
[ -x .venv/bin/python ] && PY=.venv/bin/python
[ -x .venv/Scripts/python.exe ] && PY=.venv/Scripts/python.exe
exec "$PY" app/prospects_import.py "$@"
