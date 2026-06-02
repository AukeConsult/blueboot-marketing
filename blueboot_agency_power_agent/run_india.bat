@echo off
REM ============================================================
REM  India pipeline — discover, enrich, locate, export
REM  Run from project root:  run_india.bat
REM ============================================================

call .venv\Scripts\activate.bat

echo.
echo ============================================================
echo  STEP 1 — Discover sites (Bing search + crawl + contacts)
echo ============================================================
python app\site_agent.py --countries IN
if errorlevel 1 goto :error

echo.
echo ============================================================
echo  STEP 2 — AI classify each site (sector, platform, summary)
echo ============================================================
python app\site_enrich_agent.py --countries IN
if errorlevel 1 goto :error

echo.
echo ============================================================
echo  STEP 3 — Enrich contacts (Brave + GPT: occupation, LinkedIn)
echo ============================================================
python app\site_contact_enrich.py --countries IN
if errorlevel 1 goto :error

echo.
echo ============================================================
echo  STEP 4 — Infer company locations (city, region)
echo ============================================================
python app\site_location_enrich.py --countries IN
if errorlevel 1 goto :error


echo.
echo ============================================================
echo  DONE — check exports\ folder for output files
echo ============================================================
goto :end

:error
echo.
echo ERROR: step failed with exit code %errorlevel%
echo Check output above for details.
exit /b %errorlevel%

:end
