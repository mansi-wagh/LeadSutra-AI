from __future__ import annotations

import argparse
import asyncio

from .browser import BrowserConfig
from .orchestrator import ExtractionMode, ScraperOrchestrator


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LeadSutra business scraper")
    parser.add_argument("--query", required=True, help="Business category or search phrase")
    parser.add_argument("--location", required=True, help="Location to search")
    parser.add_argument("--limit", type=int, default=50, help="Maximum business records")
    parser.add_argument("--qualification", choices=("qualified", "needs_review", "not_qualified", "mixed"),
                        default="mixed", help="Filter by lead score qualification and rank matches by score")
    parser.add_argument("--candidate-limit", type=int,
                        help="Maximum candidates scored before filtering (default: at least limit+20 or 5x limit, up to 60 when limit <= 60)")
    parser.add_argument("--mode", choices=[item.value for item in ExtractionMode], default="basic")
    parser.add_argument("--module", action="append", default=[], help="CUSTOM module; repeatable")
    parser.add_argument("--field", action="append", default=[], help="CUSTOM field; repeatable")
    parser.add_argument("--headed", action="store_true", help="Show browser windows")
    parser.add_argument("--verbose", action="store_true", help="Show per-business extraction statuses")
    parser.add_argument("--output-dir", default="scraper_outputs", help="Output root directory")
    return parser


async def _run(args: argparse.Namespace) -> None:
    app = ScraperOrchestrator(
        browser_config=BrowserConfig(headless=not args.headed),
        output_root=args.output_dir,
        progress=print,
        verbose=args.verbose,
    )
    await app.run(
        args.query, args.location, args.limit, args.mode,
        custom_modules=args.module, custom_fields=args.field,
        qualification=args.qualification, candidate_limit=args.candidate_limit,
    )


def main() -> None:
    asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    main()
