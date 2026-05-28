@echo off
setlocal
cd /d "%~dp0"

python app\site_agent.py --countries DK
python app\site_enrich_agent.py --countries DK

python app\site_agent.py --countries FI
python app\site_enrich_agent.py --countries FI

python app\site_agent.py --countries SE
python app\site_enrich_agent.py --countries SE

python app\site_agent.py --countries NO
python app\site_enrich_agent.py --countries NO

echo.
echo ============================================================
echo  ALL DONE
echo ============================================================
