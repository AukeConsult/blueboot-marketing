@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
call .venv\Scripts\activate.bat

echo.
echo ============================================================
echo  FULL DRY-RUN TEST
echo ============================================================

set COUNTRIES=SE
set PASS=0
set FAIL=0

echo.
echo [TEST 1/9] Config and credentials...
python tests\test_config.py
if %errorlevel% neq 0 ( echo   FAIL & set /a FAIL=FAIL+1 ) else ( echo   PASS & set /a PASS=PASS+1 )

echo.
echo [TEST 2/9] Firestore connection...
python tests\test_firestore.py
if %errorlevel% neq 0 ( echo   FAIL & set /a FAIL=FAIL+1 ) else ( echo   PASS & set /a PASS=PASS+1 )

echo.
echo [TEST 3/9] OpenAI connection...
python tests\test_openai.py
if %errorlevel% neq 0 ( echo   FAIL & set /a FAIL=FAIL+1 ) else ( echo   PASS & set /a PASS=PASS+1 )

echo.
echo [TEST 4/9] Brave Search API...
python tests\test_brave.py
if %errorlevel% neq 0 ( echo   FAIL & set /a FAIL=FAIL+1 ) else ( echo   PASS & set /a PASS=PASS+1 )

echo.
echo [TEST 5/9] site_agent dry run (5 results)...
python app\site_agent.py --countries %COUNTRIES% --dry-run --max-results 5
if %errorlevel% neq 0 ( echo   FAIL & set /a FAIL=FAIL+1 ) else ( echo   PASS & set /a PASS=PASS+1 )

echo.
echo [TEST 6/9] site_enrich_agent dry run (3 leads)...
python app\site_enrich_agent.py --countries %COUNTRIES% --dry-run --limit 3
if %errorlevel% neq 0 ( echo   FAIL & set /a FAIL=FAIL+1 ) else ( echo   PASS & set /a PASS=PASS+1 )

echo.
echo [TEST 7/9] lead_agent catalog dry run...
python app\lead_agent.py --countries %COUNTRIES% --mode catalog --max-catalog-pages 1 --no-firebase --no-output
if %errorlevel% neq 0 ( echo   FAIL & set /a FAIL=FAIL+1 ) else ( echo   PASS & set /a PASS=PASS+1 )

echo.
echo [TEST 8/9] site_smart_export dry run contacts...
python app\site_smart_export.py --countries %COUNTRIES% --dry-run-contacts
if %errorlevel% neq 0 ( echo   FAIL & set /a FAIL=FAIL+1 ) else ( echo   PASS & set /a PASS=PASS+1 )

echo.
echo [TEST 9/9] email_contacts_export...
python app\email_contacts_export.py --countries %COUNTRIES% --out exports\test_dry_run.xlsx
if %errorlevel% neq 0 ( echo   FAIL & set /a FAIL=FAIL+1 ) else ( echo   PASS & set /a PASS=PASS+1 )

echo.
echo ============================================================
echo  RESULTS: %PASS% passed  /  %FAIL% failed
if %FAIL% gtr 0 (
    echo  Some tests FAILED - check output above
) else (
    echo  ALL TESTS PASSED - pipeline is healthy
)
echo ============================================================
pause
