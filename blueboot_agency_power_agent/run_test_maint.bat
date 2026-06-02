@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
call .venv\Scripts\activate.bat

echo.
echo ============================================================
echo  MAINTENANCE SCRIPTS DRY-RUN TEST
echo ============================================================

set COUNTRIES=SE
set PASS=0
set FAIL=0

echo.
echo [TEST 1/8] maint_site_excluded_recheck -- dry-run...
python app\maint_site_excluded_recheck.py --countries %COUNTRIES% --dry-run --limit 5
if %errorlevel% neq 0 ( echo   FAIL & set /a FAIL=FAIL+1 ) else ( echo   PASS & set /a PASS=PASS+1 )

echo.
echo [TEST 2/8] maint_site_sitemap_backfill -- dry-run...
python app\maint_site_sitemap_backfill.py --countries %COUNTRIES% --dry-run --limit 5
if %errorlevel% neq 0 ( echo   FAIL & set /a FAIL=FAIL+1 ) else ( echo   PASS & set /a PASS=PASS+1 )

echo.
echo [TEST 3/8] maint_site_leads_export -- dry-run...
python app\maint_site_leads_export.py --countries %COUNTRIES% --dry-run
if %errorlevel% neq 0 ( echo   FAIL & set /a FAIL=FAIL+1 ) else ( echo   PASS & set /a PASS=PASS+1 )

echo.
echo [TEST 4/8] maint_fix_contact_country -- dry-run...
python app\maint_fix_contact_country.py --dry-run
if %errorlevel% neq 0 ( echo   FAIL & set /a FAIL=FAIL+1 ) else ( echo   PASS & set /a PASS=PASS+1 )

echo.
echo [TEST 5/8] maint_fix_rescrape_contacts -- dry-run...
python app\maint_fix_rescrape_contacts.py --country %COUNTRIES% --dry-run --limit 5
if %errorlevel% neq 0 ( echo   FAIL & set /a FAIL=FAIL+1 ) else ( echo   PASS & set /a PASS=PASS+1 )

echo.
echo [TEST 6/8] maint_firestore_snapshot -- keyword search (read only)...
python app\maint_firestore_snapshot.py wordpress --limit 3
if %errorlevel% neq 0 ( echo   FAIL & set /a FAIL=FAIL+1 ) else ( echo   PASS & set /a PASS=PASS+1 )

echo.
echo [TEST 7/8] maint_statistics -- read only, no writes...
python app\maint_statistics.py --no-excel --no-writeback --no-overview --only priority
if %errorlevel% neq 0 ( echo   FAIL & set /a FAIL=FAIL+1 ) else ( echo   PASS & set /a PASS=PASS+1 )

echo.
echo [TEST 8/8] maint_firestore_index_sync -- dry-run...
python app\maint_firestore_index_sync.py --dry-run
if %errorlevel% neq 0 ( echo   FAIL & set /a FAIL=FAIL+1 ) else ( echo   PASS & set /a PASS=PASS+1 )

echo.
echo ============================================================
echo  RESULTS: %PASS% passed  /  %FAIL% failed
if %FAIL% gtr 0 (
    echo  Some tests FAILED - check output above
) else (
    echo  ALL MAINTENANCE TESTS PASSED
)
echo ============================================================
pause
