# Coding Rules for this Project

## Async / asyncio

### RULE: Every `run_in_executor` call for I/O MUST have an `asyncio.wait_for` timeout

When wrapping a synchronous blocking call (Firestore, database, HTTP, file I/O) in
`loop.run_in_executor`, the underlying thread cannot be cancelled. If the I/O hangs
indefinitely, the awaiting coroutine is stuck — and if that coroutine is a consumer in a
producer/consumer pipeline, it will never process its shutdown sentinel, causing the
entire `asyncio.gather(*tasks)` to hang forever.

**Always write:**
```python
await asyncio.wait_for(
    loop.run_in_executor(None, lambda: blocking_call(...)),
    timeout=12.0,   # or another appropriate value
)
```

**Never write:**
```python
await loop.run_in_executor(None, lambda: blocking_call(...))   # NO TIMEOUT — can hang forever
```

This bug was discovered in `site_agent.py`: `upsert_site_excluded` (Firestore write) had no
timeout. On a slow connection the consumer hung, sentinels were never processed, and
`asyncio.gather(*consumer_tasks)` never returned.

---

### RULE: Executor calls for external searches also need a timeout

Wrap `run_in_executor` calls to external APIs (e.g. Bing search) in `asyncio.wait_for` too:

```python
urls = await asyncio.wait_for(
    loop.run_in_executor(None, lambda: bing_search(query, n)),
    timeout=45.0,
)
```

The thread will keep running after the timeout (threads can't be cancelled), but the
coroutine moves on and the program doesn't hang.

---

### RULE: Producer/consumer pipelines — sentinel guarantee

In any `queue.get()` consumer loop, `queue.task_done()` must be called unconditionally.
Use `try/finally`:

```python
while True:
    item = await queue.get()
    try:
        if item is SENTINEL:
            break
        # ... process item ...
    except Exception as exc:
        print(f"error: {exc}")
    finally:
        queue.task_done()   # ALWAYS called, even on exception or break
```

Never put `queue.task_done()` inside the `try` body where an exception or a nested
`await` could prevent it from running.

---

## File Editing

### RULE: Never use Edit/Write tools on large Python files directly

The Edit and Write tools truncate `site_agent.py` and similar large files at a fixed byte
boundary (~line 754 / ~34 KB). Always use Python scripts via bash for structural changes:

```bash
python3 << 'PYEOF'
src = open(path).read()
src = src.replace(old, new, 1)
open(path, 'w').write(src)
PYEOF
```

After any edit to a large file, always verify:
```bash
python3 -m py_compile app/site_agent.py && echo OK
tail -5 app/site_agent.py   # confirm file is not truncated
```

If truncated, repair with:
```python
data = open(path, 'rb').read()
idx  = data.rfind(b'<last known good line>')
open(path, 'wb').write(data[:idx] + tail)
```

---

## Firebase / Firestore

- All Firestore writes in async consumers → `run_in_executor` + `asyncio.wait_for(timeout=12)`
- All Firestore reads at startup (preload) are sync and called before `asyncio.run()` — acceptable
- `firebase_admin` is sync-only; never call it directly in a coroutine without `run_in_executor`
