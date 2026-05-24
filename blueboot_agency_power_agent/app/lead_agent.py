"""Entry point — argument parsing and mode dispatch only."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from catalog_scrapers import catalog_run
from search_runner import run


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="BlueBoot Lead Agent — find & score web-design agencies",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--mode", choices=["search", "catalog"], default="search",
        help="search = Bing/Google keyword search; catalog = scrape directory listings",
    )
    parser.add_argument(
        "--countries", default=None,
        help="Comma-separated ISO codes, e.g. NO,SE,DK. Default: all configured.",
    )
    parser.add_argument(
        "--queries", default=None,
        help="Path to a queries file (overrides per-country query files).",
    )
    parser.add_argument(
        "--output", default="output",
        help="Output directory for the Excel file.",
    )
    parser.add_argument(
        "--max-results", type=int, default=int(os.getenv("MAX_RESULTS", "10")),
        help="Max search results per query.",
    )
    parser.add_argument(
        "--max-pages", type=int, default=int(os.getenv("MAX_PAGES", "3")),
        help="Max pages to crawl per agency website.",
    )
    parser.add_argument(
        "--max-country", type=int, default=int(os.getenv("MAX_COUNTRY", "0")) or None,
        help="Stop a country after this many leads (0 = unlimited).",
    )
    parser.add_argument(
        "--give-up-after", type=int, default=int(os.getenv("GIVE_UP_AFTER", "10")),
        help="Give up a country after this many consecutive empty queries.",
    )
    parser.add_argument(
        "--delay", type=float, default=float(os.getenv("CRAWL_DELAY", "1.0")),
        help="Seconds to wait between page fetches within one site.",
    )
    parser.add_argument(
        "--workers", type=int, default=int(os.getenv("CRAWL_WORKERS", "20")),
        help="Parallel site-crawl workers / batch size.",
    )
    parser.add_argument(
        "--max-catalog-pages", type=int, default=None,
        help="Limit pages per catalog source (for testing).",
    )
    return parser


def main() -> None:
    load_dotenv()
    args = _build_parser().parse_args()
    if args.mode == "catalog":
        catalog_run(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
