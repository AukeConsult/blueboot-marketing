"""Shared sys.path bootstrap — import this before any internal imports.

Adds the directories needed so all internal modules resolve correctly
whether the script is run from the project root, from inside app/, or
from app/collect-functions/:

    <root>/                      -> allows `from app.functions.X import`
    <root>/app/                  -> allows `from functions.X import` (fallback)
    <root>/functions-crm/        -> allows `from smart_mail import` (CRM mail jobs)
    <root>/app/functions/        -> allows `from utils import` (used by models.py)
    <root>/app/collect-functions/-> allows `from catalog_scrapers/search_runner import`
"""
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent          # app/
_root = _here.parent                             # project root

for _p in [
    str(_root),                                  # for `from app.functions.X`
    str(_here),                                  # for `from functions.X`
    str(_root / "functions-crm"),                # for `from smart_mail import`
    str(_here / "functions"),                    # for `from utils import` (models.py)
    str(_here / "collect-functions"),            # for `from catalog_scrapers import`
]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Windows asyncio: use the Selector event loop, not the default Proactor.
# The Proactor loop raises a noisy, harmless ConnectionResetError from
# `_ProactorBasePipeTransport._call_connection_lost` (socket.shutdown) when
# aiohttp closes sessions. The Selector loop avoids it and works fine for
# aiohttp/HTTP workloads. Done here so every `import _pathsetup` script gets it.
# ---------------------------------------------------------------------------
if sys.platform.startswith("win"):
    import asyncio as _asyncio
    try:
        _asyncio.set_event_loop_policy(_asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass
