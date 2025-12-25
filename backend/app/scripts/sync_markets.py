#!/usr/bin/env python
"""CLI script to sync all markets from Polymarket.

Usage:
    python -m app.scripts.sync_markets [OPTIONS]

Options:
    --all           Sync all markets (including inactive)
    --active-only   Sync only active markets (default)
    --verbose       Enable verbose logging
    --dry-run       Fetch data but don't save to database

Examples:
    # Sync active markets
    python -m app.scripts.sync_markets

    # Sync all markets including inactive
    python -m app.scripts.sync_markets --all

    # Dry run to preview
    python -m app.scripts.sync_markets --dry-run --verbose
"""
import argparse
import asyncio
import logging
import sys
from datetime import datetime

# Add parent to path for imports when running as script
if __name__ == "__main__":
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.database import get_session_context
from app.services.data_collector import DataCollector, GammaAPIClient


logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False) -> None:
    """Configure logging for CLI output."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def sync_markets(
    active_only: bool = True,
    dry_run: bool = False,
    verbose: bool = False,
) -> int:
    """Sync markets from Polymarket Gamma API.

    Args:
        active_only: Only sync active markets.
        dry_run: Fetch data but don't save.
        verbose: Enable verbose logging.

    Returns:
        Number of markets synced.
    """
    setup_logging(verbose)

    logger.info("=" * 60)
    logger.info("Polymarket Market Sync")
    logger.info("=" * 60)
    logger.info(f"Mode: {'Active markets only' if active_only else 'All markets'}")
    logger.info(f"Dry run: {dry_run}")
    logger.info("")

    start_time = datetime.now()

    if dry_run:
        # Just fetch and display without saving
        gamma = GammaAPIClient()
        try:
            logger.info("Fetching markets from Gamma API...")
            markets = await gamma.get_all_markets()

            if active_only:
                markets = [m for m in markets if m.get("active", False)]

            logger.info(f"Found {len(markets)} markets")
            logger.info("")

            if verbose:
                for i, market in enumerate(markets[:20], 1):
                    question = market.get("question", "Unknown")[:60]
                    condition_id = market.get("condition_id", "")[:12]
                    active = "Active" if market.get("active") else "Inactive"
                    volume = market.get("volume24hr", 0) or 0

                    logger.info(f"  {i:3d}. [{condition_id}...] {question}...")
                    logger.info(f"       Status: {active} | 24h Volume: ${volume:,.2f}")

                if len(markets) > 20:
                    logger.info(f"  ... and {len(markets) - 20} more markets")

            synced = len(markets)

        finally:
            await gamma.close()

    else:
        # Actually sync to database
        async with get_session_context() as session:
            collector = DataCollector(session)
            try:
                logger.info("Syncing markets to database...")
                synced = await collector.sync_markets(active_only=active_only)
            finally:
                await collector.close()

    elapsed = (datetime.now() - start_time).total_seconds()

    logger.info("")
    logger.info("-" * 60)
    logger.info(f"Completed in {elapsed:.2f} seconds")
    logger.info(f"Markets synced: {synced}")
    logger.info("=" * 60)

    return synced


def main() -> None:
    """Main entry point for CLI."""
    parser = argparse.ArgumentParser(
        description="Sync Polymarket markets to database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                    Sync active markets
  %(prog)s --all              Sync all markets
  %(prog)s --dry-run          Preview without saving
  %(prog)s --verbose          Show detailed output
        """,
    )

    parser.add_argument(
        "--all",
        action="store_true",
        dest="include_all",
        help="Sync all markets including inactive",
    )
    parser.add_argument(
        "--active-only",
        action="store_true",
        default=True,
        help="Sync only active markets (default)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch data but don't save to database",
    )

    args = parser.parse_args()

    # --all overrides --active-only
    active_only = not args.include_all

    try:
        result = asyncio.run(
            sync_markets(
                active_only=active_only,
                dry_run=args.dry_run,
                verbose=args.verbose,
            )
        )
        sys.exit(0 if result > 0 else 1)

    except KeyboardInterrupt:
        logger.info("\nInterrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Error: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
