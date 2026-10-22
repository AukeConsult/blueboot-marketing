@echo off
setlocal
call .venv\Scripts\activate.bat
python app\check_sent_totals.py %*
endlocal
