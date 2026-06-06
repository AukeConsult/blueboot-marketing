# Code Rules (from CLAUDE.md)

## NEVER use Edit/Write tools on large Python files
Files like main.py, site_agent.py truncate at ~34KB. Always use Python via bash:
```bash
python3 - << 'PY'
src = open(path).read()
src = src.replace(old, new, 1)
open(path, "w").write(src)
PY
python3 -m py_compile file.py && tail -3 file.py
```

## NEVER use Edit tool on large HTML files either
Same truncation problem. For large HTML files use Python replace scripts or bash append.
crm-common.js and large pages (campaign.html, settings.html) have been truncated before.

## After every Python edit run both
```bash
python3 -m py_compile app/*.py
python3 -m pyflakes app/*.py | grep "undefined name"
```

## Async rules
- Every run_in_executor call MUST have asyncio.wait_for timeout
- Producer/consumer: queue.task_done() in finally block always
- Top-level coroutines need hard ceiling timeout

## ElementTree
Never use `or` to chain Element.find() — falsy elements break it

## Thread safety
- Firestore singleton uses double-checked locking
- _write_exec.shutdown(wait=False) only — never wait=True in async def

## Frontend
- Always Bootstrap 5 + Tabler Icons
- Load from vendor/ (local), not CDN
- crm-common.js defines BASE, escapeHtml, nav — must be complete/untruncated
