@echo off
setlocal
cd /d "%~dp0"
call .venv\Scripts\activate.bat

REM ============================================================
REM  SITE ENRICHMENT PIPELINE (steps 2-6, no discovery)
REM  Runs on already-discovered site_leads.
REM  Edit COUNTRIES and CAMPAIGN before running.
REM ============================================================

set COUNTRIES=NO
set CAMPAIGN=NO_jun02

echo.
echo ============================================================
echo  SITE ENRICH  ^|  Countries: %COUNTRIES%
echo ============================================================

REM ── Step 1: AI classify each site ───────────────────────────
echo.
echo [1/5] AI classifying sites...
python app\site_enrich_agent.py --countries %COUNTRIES%
if %errorlevel% neq 0 ( echo ERROR in site_enrich_agent.py & pause & exit /b 1 )

REM ── Step 2: Enrich contacts via Brave + GPT ─────────────────
echo.
echo [2/5] Enriching contacts...
python app\site_contact_enrich.py --countries %COUNTRIES%
if %errorlevel% neq 0 ( echo ERROR in site_contact_enrich.py & pause & exit /b 1 )

REM ── Step 3: Location enrichment ─────────────────────────────
echo.
echo [3/5] Enriching location...
python app\site_location_enrich.py --countries %COUNTRIES%
if %errorlevel% neq 0 ( echo ERROR in site_location_enrich.py & pause & exit /b 1 )

REM ── Step 4: Classify email type ─────────────────────────────
echo.
echo [4/5] Classifying email types...
python app\site_email_check.py --countries %COUNTRIES%
if %errorlevel% neq 0 ( echo ERROR in site_email_check.py & pause & exit /b 1 )

REM ── Step 5: Export to email_contacts ────────────────────────
echo.
echo [5/5] Exporting to email_contacts...
python app\site_smart_export.py --countries %COUNTRIES% --write-contacts --campaign %CAMPAIGN%
if %errorlevel% neq 0 ( echo ERROR in site_smart_export.py & pause & exit /b 1 )

echo.
echo ============================================================
echo  DONE  ^|  Countries: %COUNTRIES%
echo ============================================================
pause
