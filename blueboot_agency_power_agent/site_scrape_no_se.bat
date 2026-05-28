@echo off
setlocal

cd /d "%~dp0"

echo ============================================================
echo  STEP 1 -- Site Agent: Norway + Sweden
echo ============================================================
python app\site_agent.py --countries NO,SE

if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] site_agent.py failed with exit code %ERRORLEVEL%
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo ============================================================
echo  STEP 2 -- Site Enricher: Norway + Sweden
echo ============================================================
python app\site_enrich_agent.py --countries NO,SE

if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] site_enrich_agent.py failed with exit code %ERRORLEVEL%
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo ============================================================
echo  ALL DONE
echo ============================================================
pause
