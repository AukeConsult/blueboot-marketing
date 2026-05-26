"""Shared sys.path bootstrap — import this before any internal imports.

Adds the directories needed so all internal modules resolve correctly
whether the script is run from the project root, from inside app/, or
from app/collect-functions/:

    <root>/                      -> allows `from app.functions.X import`
    <root>/app/                  -> allows `from functions.X import` (fallback)
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
    str(_here / "functions"),                    # for `from utils import` (models.py)
    str(_here / "collect-functions"),            # for `from catalog_scrapers import`
]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
