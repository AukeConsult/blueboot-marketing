@echo off
setlocal
call .venv\Scripts\activate.bat
python app\reply_match.py %*
endlocal
