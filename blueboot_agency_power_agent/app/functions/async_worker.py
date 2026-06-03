"""Reusable async building blocks for the scraping/enrichment pipelines.

Two pieces, both designed to remove single points of failure:

* ``BoundedFetcher`` — the ONE place HTTP bodies are read. Every read is size-capped
  (body + decompressed gzip) and time-bounded, and it NEVER raises — a bad response
  yields "" instead of propagating. This replaces ad-hoc per-module read functions.

* ``Worker`` — an isolated unit of work. ``run()`` wraps ``process()`` in a hard
  ``asyncio.wait_for`` and a catch-all, so a single worker can neither hang the event
  loop past its timeout nor crash its siblings. It always returns a ``WorkerResult``.

Keep all body reads going through ``BoundedFetcher`` and all per-item work wrapped in a
``Worker`` subclass — see CLAUDE.md (async rules) for why.
"""
from __future__ import annotations

import asyncio
import gzip as _gzip
import io as _io
from dataclasses import dataclass
from typing import Any

import aiohttp

DEFAULT_MAX_BODY = 8_000_000   # 8 MB hard cap on raw body bytes
DEFAULT_MAX_TEXT = 3_000_000   # 3 MB cap on decoded text handed to parsers


class BoundedFetcher:
    """Single, safe HTTP read path. Construct once per aiohttp session."""

    def __init__(self, session: aiohttp.ClientSession,
                 *, max_body: int = DEFAULT_MAX_BODY, max_text: int = DEFAULT_MAX_TEXT,
                 headers: dict | None = None, xml_headers: dict | None = None):
        self._session = session
        self._max_body = max_body
        self._max_text = max_text
        self._headers = dict(headers or {})
        self._xml_headers = dict(xml_headers or self._headers)

    async def get(self, url: str, *, timeout: float = 15.0, xml: bool = False,
                  return_final_url: bool = False):
        """Bounded, never-raising GET. Returns decoded text (or ("", url) tuple).

        Body and decompressed gzip are both capped so a huge/hostile response can
        never block the event loop in a synchronous decode/parse.
        """
        headers = dict(self._xml_headers if xml else self._headers)
        if xml:
            headers.setdefault("Accept", "application/xml,text/xml,*/*;q=0.8")
        empty = ("", url) if return_final_url else ""
        try:
            async with self._session.get(
                url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=True, ssl=False,
            ) as resp:
                final_url = str(resp.url)
                if resp.status != 200:
                    return empty
                raw = await resp.content.read(self._max_body + 1)   # bounded read
                if len(raw) > self._max_body:
                    raw = raw[:self._max_body]
                if raw[:2] == b"\x1f\x8b":                          # gzip — cap output too
                    try:
                        with _gzip.GzipFile(fileobj=_io.BytesIO(raw)) as gz:
                            raw = gz.read(self._max_body)
                    except Exception:
                        return empty
                text = raw.decode("utf-8", errors="replace")[:self._max_text]
                if xml:
                    stripped = text.lstrip("﻿").lstrip()
                    if not (stripped.startswith("<?xml")
                            or stripped.startswith("<sitemapindex")
                            or stripped.startswith("<urlset")):
                        return empty
                return (text, final_url) if return_final_url else text
        except Exception:
            return empty


@dataclass
class WorkerResult:
    worker_id: str
    status: str               # "ok" | "excluded" | "error" | "timeout"
    value: Any = None
    reason: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class Worker:
    """Isolated unit of work.

    Subclass and implement ``process() -> WorkerResult``. Call ``run()`` — it wraps
    ``process()`` in a hard timeout and a catch-all, so it NEVER raises and NEVER
    runs longer than ``timeout``. One worker failing or timing out therefore cannot
    affect any sibling running concurrently.
    """

    def __init__(self, worker_id: str, *, timeout: float = 120.0):
        self.worker_id = worker_id
        self.timeout = timeout

    async def process(self) -> WorkerResult:               # pragma: no cover
        raise NotImplementedError

    async def run(self) -> WorkerResult:
        try:
            return await asyncio.wait_for(self.process(), timeout=self.timeout)
        except asyncio.TimeoutError:
            return WorkerResult(self.worker_id, "timeout", reason="timeout")
        except Exception as exc:
            return WorkerResult(self.worker_id, "error", error=str(exc))
