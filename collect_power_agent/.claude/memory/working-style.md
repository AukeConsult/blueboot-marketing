# Working Style — Leif Auke

## Communication
- Very concise, direct. Dislikes verbosity.
- Says "show me first" before making changes → read/show code before editing
- Sends empty messages sometimes (fat-finger) — just ask what they need
- Uses shorthand: "make a popup", "add a selector", "call it X"

## Code preferences
- Python for backend edits on large files (never Edit tool on big .py or .html)
- Bootstrap 5 + Tabler Icons always
- Inline saves preferred (auto-save on change, 1.2s debounce)
- Modals for confirmations on destructive actions
- Status line > stat cards (compact display)
- Single source of truth: extract repeated logic into classes (e.g. MailSender)

## Workflow
- Shows pages one at a time, makes targeted UI changes
- Asks "show me" before changes on complex pages
- Prefers incremental improvements over rewrites
- Will say "how is that?" when surprised by something still being there

## Key cautions
- Edit tool truncates large files — ALWAYS use Python scripts for main.py, campaign.html, settings.html
- crm-common.js truncation breaks ALL pages (BASE undefined, nav gone) — highest risk file
- After any HTML edit, verify file ends with </html>
- Deploy with: firebase deploy --only hosting (pages) or firebase deploy --only functions:crm (backend)
