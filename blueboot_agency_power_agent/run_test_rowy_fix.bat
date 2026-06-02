@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
call .venv\Scripts\activate.bat

echo.
echo ============================================================
echo  TEST: _rowy_ fix + collection default fix
echo ============================================================

set COUNTRIES=SE
set PASS=0
set FAIL=0

echo.
echo [TEST 1/3] maint_fix_rescrape_contacts -- dry-run...
python app\maint_fix_rescrape_contacts.py --country %COUNTRIES% --dry-run --limit 5
if %errorlevel% neq 0 ( echo   FAIL & set /a FAIL=FAIL+1 ) else ( echo   PASS & set /a PASS=PASS+1 )

echo.
echo [TEST 2/3] maint_firestore_snapshot -- keyword search...
python app\maint_firestore_snapshot.py wordpress --limit 3
if %errorlevel% neq 0 ( echo   FAIL & set /a FAIL=FAIL+1 ) else ( echo   PASS & set /a PASS=PASS+1 )

echo.
echo [TEST 3/3] maint_statistics -- read only...
python app\maint_statistics.py --no-excel --no-writeback --no-overview --only priority
if %errorlevel% neq 0 ( echo   FAIL & set /a FAIL=FAIL+1 ) else ( echo   PASS & set /a PASS=PASS+1 )

echo.
echo ============================================================
echo  RESULTS: %PASS% passed  /  %FAIL% failed
if %FAIL% gtr 0 (
    echo  Some tests FAILED - check output above
) else (
    echo  ALL PASSED
)
echo ============================================================
pause
