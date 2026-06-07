# Coding Rules for this Project

## THE OVERARCHING RULE: parallel work → isolated classes

Whenever you create parallel processes — whether with `asyncio` (gather, queues,
producer/consumer) or with multiple threads (`ThreadPoolExecutor`, `threading.Thread`)
— ALWAYS wrap each unit of work in its own isolated class.

Each such class must:

- own all of its mutable state (no shared globals/dicts/sets/counters across units),
- never raise out of its public entry point (catch-all → return a result object),
- never run past a hard timeout (`asyncio.wait_for` for coroutines; bounded waits for
  threads),
- route every I/O / body read through a single shared, capped, never-raising helper
  class (e.g. `BoundedFetcher`).

The goal: one unit failing, hanging, or timing out can NEVER stall, crash, or corrupt
any sibling. The reference implementation lives in `app/functions/async_worker.py`
(`BoundedFetcher`, `Worker`/`WorkerResult`) with `site_agent.py`
(`SiteWorker`, `SitemapReader`) as the worked example. Every rule below is a concrete
consequence of this one.

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

### RULE: Async coroutines that call multiple awaits MUST have a top-level timeout

A coroutine that chains several `await` calls (e.g. fetch robots.txt → fetch N sitemaps →
fetch homepage → fetch contact pages) can take far longer than any single call's timeout.
Even though each inner `_async_get` has its own `timeout=N`, the **total** is unbounded.
If that coroutine runs inside a consumer, it can block the consumer indefinitely.

**Always wrap high-level async worker functions in `asyncio.wait_for`:**
```python
lead, excl_reason = await asyncio.wait_for(
    process_site_async(session, url, ...),
    timeout=120.0,   # hard ceiling for the entire site-processing chain
)
```

This bug caused the final `asyncio.gather(*consumer_tasks)` to hang after the last
printed item: one consumer was stuck inside `process_site_async` with no escape.
The per-call aiohttp timeouts protect individual requests, not the whole chain.

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

---

## ElementTree

### RULE: Never use `or` to chain `Element.find()` calls

`xml.etree.ElementTree.Element` evaluates as **falsy** when it has no child elements,
even if it exists and has text content. A `<loc>` or `<lastmod>` node has text but no
children, so `bool(element)` is `False`.

**Never write:**
```python
loc = sm.find(f"{{{ns}}}loc") or sm.find("loc")   # WRONG — drops valid results
```

**Always write:**
```python
loc = sm.find(f"{{{ns}}}loc")
if loc is None:
    loc = sm.find("loc")
```

This bug caused `_index_entries` to return empty `children` lists for all sitemapindex
nodes, making every site report `pages=0 (index)`.

---

## Verification

### RULE: `py_compile` is NOT enough — always run `pyflakes` for undefined names

`python -m py_compile` only catches **syntax** errors. It passes on undefined-name bugs
that crash at runtime — these keep recurring in this project:

- `cfg.OPENAI_MODEL` used at module scope while `cfg` was only imported locally inside a
  function (`site_enrich_agent.py`)
- `_local_fb_lock` referenced but never defined (20 files at once)
- `cred` used in `initialize_app(cred)` while the cert was assigned to `c`
- `normalize_url(...)` used but never imported from `functions.utils` (`lead_agent.py`)

**After ANY edit, run both:**
```bash
python3 -m py_compile app/*.py
python3 -m pyflakes app/*.py | grep -i "undefined name"   # must print nothing
```
If `pyflakes` reports an undefined name, fix it before considering the task done.

### RULE: Never edit large files with the Edit/Write tools — they truncate

The Edit/Write tools silently truncate large files (`site_agent.py`, `lead_agent.py`,
`site_email_check.py`) mid-file, producing `SyntaxError: unterminated string literal` /
`'(' was never closed` at the tail. Always edit via a Python script in bash:
```bash
python3 - << 'PY'
p = "app/lead_agent.py"; s = open(p, encoding="utf-8").read()
s = s.replace(OLD, NEW, 1)
open(p, "w", encoding="utf-8").write(s)
PY
python3 -m py_compile app/lead_agent.py && tail -3 app/lead_agent.py   # confirm not truncated
```

---

## Thread Safety

These scripts run blocking Firestore/Firebase calls inside `ThreadPoolExecutor`
threads. Every variable touched by more than one thread MUST be protected by a
`threading.Lock`. (asyncio counters like `counters["done"] += 1` inside a coroutine
are safe without locks — asyncio is single-threaded; only `ThreadPoolExecutor` code
needs locks.)

### RULE: singletons use double-checked locking

`app/firestore_client.py` has a global `_db` singleton. Concurrent `_write_exec`
threads calling `get_firestore()` can double-init and corrupt the connection pool →
hangs. Never remove or bypass the lock.

```python
_singleton = None
_lock = threading.Lock()

def get_singleton():
    if _singleton is not None:        # fast path, no lock
        return _singleton
    with _lock:
        if _singleton is not None:    # re-check inside lock
            return _singleton
        _singleton = create_it()
    return _singleton
```

`initialize_app` in every `app/*.py` must be wrapped with `_local_fb_lock`
(`_threading.Lock()` defined at module top). Audit after adding any file:

```bash
python3 -c "
import os, re
for root, dirs, files in os.walk('app'):
    dirs[:] = [d for d in dirs if d != '__pycache__']
    for f in files:
        if not f.endswith('.py'): continue
        src = open(os.path.join(root,f), errors='replace').read()
        lines = src.splitlines()
        for i, l in enumerate(lines):
            if ('initialize_app' in l or 'firestore.client()' in l) and not l.strip().startswith('#'):
                ctx = chr(10).join(lines[max(0,i-30):i])
                if not re.search(r'with\s+_\w*lock\w*\s*:', ctx):
                    print(f'UNPROTECTED {root}/{f}:{i+1}  {l.strip()[:70]}')
"
```

### RULE: `_write_exec` writes are awaited with a timeout — never fire-and-forget

```python
# WRONG — no backpressure, floods the pool when Firestore is slow
_write_exec.submit(lambda: upsert_site_lead(lead, col))

# CORRECT — bounded consumer
await asyncio.wait_for(
    loop.run_in_executor(_write_exec, lambda _l=lead: upsert_site_lead(_l, col)),
    timeout=20.0,
)
```

Set the pool size to the number of consumer workers so every consumer can submit
at once: `ThreadPoolExecutor(max_workers=max(workers, 8))`.

### RULE: never `shutdown(wait=True)` inside an `async def`

```python
_write_exec.shutdown(wait=True)    # WRONG — freezes the event loop
_write_exec.shutdown(wait=False)   # CORRECT — threads drain in background
```

### RULE: `gather(*tasks)` fan-out helpers need a per-worker `wait_for`

Helpers that build `tasks = [...]` then `await asyncio.gather(*tasks, return_exceptions=True)`
(`fix_rescrape_contacts.py` `_recrawl_one`, `lead_enrich_contacts.py` `_enrich_one`)
will hang forever if one worker's chained awaits never return — `return_exceptions=True`
only catches *raised* exceptions, not a coroutine that never returns. Wrap each worker
in `asyncio.wait_for`. Audit: `grep -c wait_for` == 0 while `grep -c gather` >= 1 ⇒ suspect.

### Health check — run after editing ANY .py file

```bash
python3 -c "
import os, subprocess, ast
issues = []
for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in ('__pycache__', '.venv', 'venv', '.git', 'node_modules')]
    for f in sorted(files):
        if not f.endswith('.py'): continue
        path = os.path.join(root, f)
        src  = open(path, errors='replace').read()
        if len(src) < 100: continue
        r = subprocess.run(['python3','-m','py_compile', path], capture_output=True)
        if r.returncode != 0:
            issues.append(f'COMPILE  {path}'); continue
        try:
            tree = ast.parse(src)
            has_main = any(isinstance(n, ast.FunctionDef) and n.name == 'main' for n in ast.walk(tree))
            if has_main and 'if __name__' not in src:
                issues.append(f'NO_ENTRY {path}')
        except: pass
        if not src.endswith('\n'):
            issues.append(f'NO_NL    {path}')   # truncated mid-write (Edit/Write byte limit)
print('ALL OK' if not issues else chr(10).join(issues))
"
```

`COMPILE` = syntax/import error. `NO_ENTRY` = `main()` defined but no
`if __name__ == "__main__": main()` (script exits silently doing nothing).
`NO_NL` = file truncated mid-write — repair the tail.

---

## Background worker threads (queue-drain pattern)

`search_runner.py` `_BackgroundCrawler` runs crawl batches on a daemon thread fed
by a `queue.Queue`. Two bugs caused the recurring
`[bg-crawl] wait() timed out after 300s` and silently dropped sites.

### RULE: drain on real outstanding work, never on `thread.is_alive()`

A daemon worker sits in `while True: queue.get()` so it is **always alive** until it
processes a shutdown sentinel. A drain loop gated on `thread.is_alive()` therefore
never exits early and spins the entire timeout on every call.

```python
# WRONG — always-alive worker means this spins the full timeout every time
while not self._queue.empty() or self._thread.is_alive():
    ...

# CORRECT — poll the queue's own outstanding-work counter
while self._queue.unfinished_tasks > 0:   # ++ on put, -- on task_done
    ...
```

### RULE: draining and shutdown are separate operations

A mid-run drain MUST keep the worker alive so the run can submit more work after it.
Only a single terminal `close()` may send the sentinel and `join()` the thread.

```python
def drain(self, timeout=300.0):   # non-destructive — call as often as needed
    while self._queue.unfinished_tasks > 0:
        if time.monotonic() > deadline:
            print("  [bg] still draining — waiting…")   # heartbeat, NOT give-up
            deadline = time.monotonic() + timeout       # re-arm, never abandon
        time.sleep(0.5)

def close(self, timeout=300.0):   # call ONCE at end of run
    self.drain(timeout)
    self._queue.put_nowait(None)  # sentinel
    self._thread.join(timeout=10.0)
```

A destructive `wait()` that sends the sentinel mid-run kills the worker; every later
`submit()` then queues work nothing consumes, and the next drain hits the timeout and
loses those items. Keep `wait()` only as a non-destructive alias for `drain()`.

### RULE: a drain timeout is a heartbeat, not a give-up

Re-arm the deadline and keep waiting (or loop the drain) so a backlog larger than one
timeout window still fully drains instead of being abandoned and dropped.

---

## Secrets / return-arity consistency

### RULE: `_load_secrets()` return arity MUST match every caller's unpack

Callers unpack `_load_secrets()` differently across the project — some expect one value,
some expect two:

```python
fb_key             = _load_secrets()   # single-value callers
fb_key, api_key    = _load_secrets()   # two-value callers (most enrich/check scripts)
api_key, fb_key    = _load_secrets()   # site_enrich_agent.py — note the ORDER
```

If a `_load_secrets()` is changed to `return get_firebase_cred()` while its caller still
does `fb_key, api_key = _load_secrets()`, it fails at runtime with
`TypeError: cannot unpack non-iterable Certificate object`. `py_compile` and `pyflakes`
do NOT catch this.

**For two-value callers** (OpenAI key resolved from `cfg`/env at the call site) return:
```python
return get_firebase_cred(), None   # (firebase_cred, openai_key)
```

After editing any `_load_secrets()` or its caller, grep both ends and confirm they agree:
```bash
grep -n "= _load_secrets()" app/*.py        # caller unpack arity
grep -n "return .*get_firebase_cred"  app/*.py   # definition return arity
```

### RULE: `_init_firestore` must accept an already-built credential — never re-wrap

`_load_secrets()` returns `get_firebase_cred()`, which is already a
`firebase_admin.credentials.Certificate` object. Passing it back into
`creds.Certificate(fb_key)` raises at runtime:
`ValueError: Invalid certificate argument ... must be a file path, or a dict`.

`_init_firestore` must handle both a ready credential object and a dict/path:
```python
import firebase_admin.credentials as creds   # or 'as fb_creds'
cred = fb_key if isinstance(fb_key, creds.Base) else creds.Certificate(fb_key)
# else / default branch may still build from a path:
#   creds.Certificate(cfg.FIREBASE_CREDENTIALS or "config/serviceAccountKey.json")
```
`Certificate` subclasses `credentials.Base`, so the `isinstance(..., creds.Base)`
guard accepts any credential object while still wrapping a raw dict/path.
`py_compile` and `pyflakes` do NOT catch this — it only fails at run time.

---

## Windows asyncio event loop

### RULE: keep the Windows Selector event-loop policy in `_pathsetup.py`

On Windows the default Proactor event loop raises a noisy but harmless
`ConnectionResetError` from `_ProactorBasePipeTransport._call_connection_lost`
(socket.shutdown) when aiohttp closes sessions:

```
File ".../asyncio/proactor_events.py", line 165, in _call_connection_lost
    self._sock.shutdown(socket.SHUT_RDWR)
```

`app/_pathsetup.py` switches Windows to the Selector loop (works fine for
aiohttp/HTTP) so the traceback never appears:

```python
if sys.platform.startswith("win"):
    import asyncio as _asyncio
    _asyncio.set_event_loop_policy(_asyncio.WindowsSelectorEventLoopPolicy())
```

This MUST run before any event loop is created — it lives in `_pathsetup`, which
every script imports first (`import _pathsetup` at the top). Never remove it, and
never create/run an event loop before `import _pathsetup`.

---

## Async CPU-bound work / response size caps

### RULE: `asyncio.wait_for` cannot cancel synchronous CPU work on the loop thread

`wait_for(coro, timeout)` only fires at `await` points. If a coroutine runs a long
**synchronous** operation (huge regex, `ElementTree` parse, `gzip.decompress`, decoding
a giant body), the single event-loop thread is busy in C code and never returns to the
scheduler — so the timeout never triggers and **every** consumer/producer on that loop
freezes at once. Symptom: the last line prints, then total silence (no per-site timeout
error after N seconds).

This froze `site_agent.py` at `[1325/4231]` on a dealer site that served a gzipped
sitemap: `resp.read()` + `gzip.decompress()` expanded to hundreds of MB and the decode +
parse blocked the loop.

### RULE: always cap response body size — never read/decompress/parse an unbounded body

```python
_MAX_BODY = 8_000_000
raw = await resp.content.read(_MAX_BODY + 1)     # bounded read, NOT resp.read()
if len(raw) > _MAX_BODY:
    raw = raw[:_MAX_BODY]
if raw[:2] == b"\x1f\x8b":                        # gzip — cap the DECOMPRESSED size too
    import gzip as _gzip, io as _io
    with _gzip.GzipFile(fileobj=_io.BytesIO(raw)) as _gz:
        raw = _gz.read(_MAX_BODY)                  # NOT gzip.decompress(raw) — bomb risk
text = raw.decode("utf-8", errors="replace")[:3_000_000]
```

If genuinely heavy parsing is unavoidable, move it off the loop with
`run_in_executor` (wrapped in `wait_for`) so a slow parse can't block other coroutines.

---

## Per-process isolation: classes, not single-point functions

The pipelines must be built from small isolated classes so one unit of work can
never hang the loop, crash siblings, or share mutable state. Shared building blocks
live in `app/functions/async_worker.py`.

### RULE: all HTTP body reads go through `BoundedFetcher` — never ad-hoc read functions

`BoundedFetcher` is the ONE place a response body is read. It caps the raw body and
the decompressed gzip, time-bounds the request, and NEVER raises (returns "" on any
failure). Do not write per-module read helpers that call `resp.read()` /
`resp.text()` directly — route them through `BoundedFetcher` (or a thin adapter over
it, like `site_agent._async_get`). This keeps the size caps from drifting and removes
single points of failure.

### RULE: each unit of work is an isolated `Worker` subclass

Wrap each per-item process (one site, one contact, one batch) in a `Worker` subclass
implementing `process() -> WorkerResult`. Call `run()` — it wraps `process()` in a
hard `asyncio.wait_for` plus a catch-all, so it NEVER raises and NEVER runs past its
timeout. One worker failing or timing out therefore cannot affect any sibling.

```python
worker = SiteWorker(session, url, ..., timeout=120.0)
res = await worker.run()          # -> WorkerResult: ok | excluded | timeout | error
```

`site_agent.py` is the reference: `SiteWorker(Worker)` per site, and `SitemapReader`
(a per-site class that owns its own visited set / fetch budget / discovered sitemaps)
so concurrent site reads share no mutable state.

### RULE: parallelise per-site child-sitemap reads, bounded by the connector

Child sitemaps within a site are fetched concurrently via `asyncio.gather`
(`SitemapReader._count_children`), throttled by the aiohttp connector's
`limit_per_host` (3) so it speeds up sitemap-heavy sites without hammering the server.
Keep `return_exceptions=True` on that gather so one bad child can't abort the level.

---

## README requirements

### RULE: README.md must always contain the Outreach Pipeline Architecture figure

The `README.md` must always contain the ASCII architecture diagram showing the full
pipeline from discovery to outreach sender. It lives under the heading
`## Outreach Pipeline Architecture` near the top of the file, before the detailed
pipeline sections.

If the diagram is missing, restore it — it should show:
- SITE PIPELINE and LEAD PIPELINE converging into `email_contacts`
- CRM Pipeline step (crm/ folder)
- Excel Export + Import Back step
- Automated Outreach Sender

Also ensure `README.md` always references `crm/README.md` for the CRM module and
includes the CRM section with key URLs (dashboard, API, sheets) and project structure.

Also ensure `crm/README.md` always contains the CRM pipeline flow figure at the top,
showing:
- email_contacts → Contact Sheet → CRM Template → Firestore + site_leads
- API flow: crmApi → Cloud Tasks → crmWorker → crm_jobs
- Dashboard URL: https://blueboot-market.web.app/

---

## Frontend / HTML rules

### RULE: Always use Bootstrap for HTML pages

All HTML pages in the `public/` folder must use Bootstrap 5 for layout and UI.
Load it from CDN:

```html
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
```

Also use Tabler Icons for icons:
```html
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.19.0/dist/tabler-icons.min.css">
```

Only write custom `.css` when Bootstrap utilities cannot cover it.

---

## Frontend / CSS rules

### RULE: No inline styles on HTML elements — use CSS classes instead

Do not write `style="..."` directly on HTML elements unless there is absolutely no
other option (e.g. a truly one-off dynamic value set from JavaScript).

**Never write:**
```html
<thead style="background:#f9fafb">
<div style="font-size:.82rem;color:#6b7280;text-transform:uppercase">
```

**Always write:**
```html
<thead class="bb-thead">
<div class="bb-section-label">
```

If no suitable class exists yet, add one to `public/css/styles.css` first, then use
it. This keeps all visual decisions in one place and makes global changes trivial.

The only exceptions:
- Truly dynamic values set by JavaScript (e.g. `el.style.width = px + 'px'`)
- Bootstrap utility classes that already cover the case (prefer those over custom CSS)
