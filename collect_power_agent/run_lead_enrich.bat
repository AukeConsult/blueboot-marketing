@echo off
setlocal
cd /d "%~dp0"
call .venv\Scripts\activate.bat

REM ============================================================
REM  LEAD ENRICHMENT PIPELINE (steps 2-5, no discovery)
REM  Runs on already-discovered leads.
REM  Edit COUNTRIES and CAMPAIGN before running.
REM ============================================================

set COUNTRIES=*
set CAMPAIGN=NO_jun02

echo.
echo ============================================================
echo  LEAD ENRICH  ^|  Countries: %COUNTRIES%
echo ============================================================

REM ── Step 1: AI classify each lead ───────────────────────────
echo.
echo [1/4] AI classifying leads...
python app\lead_enrich_agent.py --countries %COUNTRIES%
if %errorlevel% neq 0 ( echo ERROR in lead_enrich_agent.py & pause & exit /b 1 )

REM ── Step 2: Enrich contacts with social profiles ─────────────
echo.
echo [2/4] Enriching contacts...
python app\lead_enrich_contacts.py --countries %COUNTRIES% --skip-enriched
if %errorlevel% neq 0 ( echo ERROR in lead_enrich_contacts.py & pause & exit /b 1 )

REM ── Step 3: Classify email type + contact role ───────────────
echo.
echo [3/4] Classifying email types...
python app\leads_email_check.py --countries %COUNTRIES%
if %errorlevel% neq 0 ( echo ERROR in leads_email_check.py & pause & exit /b 1 )

REM ── Step 4: Export to email_contacts ────────────────────────
echo.
echo [4/4] Exporting to email_contacts...
python app\leads_smart_export.py --countries %COUNTRIES% --write-contacts --campaign %CAMPAIGN%
if %errorlevel% neq 0 ( echo ERROR in leads_smart_export.py & pause & exit /b 1 )

echo.
echo ============================================================
echo  DONE  ^|  Countries: %COUNTRIES%
echo ============================================================
pause
