@echo off
call .venv\Scripts\activate.bat
python app\campaign_scrape_emails.py %*
