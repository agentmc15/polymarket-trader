#!/usr/bin/env python
"""CLI script for continuous price collection from Polymarket.

This script runs a continuous loop collecting price snapshots for all
active markets at a configurable interval.

Usage:
    python -m app.scripts.collect_prices [OPTIONS]

Options:
    --interval      Collection interval in seconds (default: 60)
    --batch-size    Markets per batch (default: 20)
    --once          Run once and exit (don't loop)
    --verbose       Enable verbose logging

Examples:
    # Collect prices every minute (default)
    python -m app.scripts.collect_prices

    # Collect prices every 30 seconds
    python -m app.scripts.collect_prices --interval 30

    # Run once for testing
    python -m app.scripts.collect_prices --once --verbose

    # High-frequency collection with smaller batches
    python -m app.scripts.collect_prices --interval 15 --batch-size 10
"""
import argparse
import asyncio
import logging
import signal
import sys
from datetime import datetime
from typing import Optional

# Add parent to path for imports when running as script
if __name__ == "__main__":
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.database import get_session_context
from app.services.data_collector import DataCollector


logger = logging.getLogger(__name__)

# Global flag for graceful shutdown
_shutdown_requested = False


def setup_logging(verbose: bool = False) -> None:
    """Configure logging for CLI output."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    global _shutdown_requested
    signal_name = signal.Signals(signum).name
    logger.info(f"\nReceived {signal_name}, shutting down gracefully...")
    _shutdown_requested = True


async def collect_prices_once(
    batch_size: int = 20,
    delay_between_batches: float = 1.0,
    verbose: bool = False,
) -> int:
    """Collect price snapshots for all active markets once.

    Args:
        batch_size: Markets to process per batch.
        delay_between_batches: Seconds between batches.
        verbose: Enable verbose logging.

    Returns:
        Number of snapshots collected.
    """
    async with get_session_context() as session:
        collector = DataCollector(session)
        try:
            collected = await collector.collect_all_prices(
                batch_size=batch_size,
                delay_between_batches=delay_between_batches,
            )
            return collected
        finally:
            await collector.close()


async def collect_prices_loop(
    interval: int = 60,
    batch_size: int = 20,
    delay_between_batches: float = 1.0,
    verbose: bool = False,
) -> None:
    """Run continuous price collection loop.

    Args:
        interval: Seconds between collection runs.
        batch_size: Markets to process per batch.
        delay_between_batches: Seconds between batches.
        verbose: Enable verbose logging.
    """
    global _shutdown_requested

    setup_logging(verbose)

    logger.info("=" * 60)
    logger.info("Polymarket Continuous Price Collector")
    logger.info("=" * 60)
    logger.info(f"Collection interval: {interval} seconds")
    logger.info(f"Batch size: {batch_size} markets")
    logger.info(f"Press Ctrl+C to stop")
    logger.info("")

    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    collection_count = 0
    total_snapshots = 0
    start_time = datetime.now()

    while not _shutdown_requested:
        collection_count += 1
        run_start = datetime.now()

        logger.info(f"[Run #{collection_count}] Starting price collection...")

        try:
            collected = await collect_prices_once(
                batch_size=batch_size,
                delay_between_batches=delay_between_batches,
                verbose=verbose,
            )
            total_snapshots += collected

            elapsed = (datetime.now() - run_start).total_seconds()
            logger.info(
                f"[Run #{collection_count}] Collected {collected} snapshots "
                f"in {elapsed:.1f}s (total: {total_snapshots:,})"
            )

        except Exception as e:
            logger.error(f"[Run #{collection_count}] Error: {e}")
            if verbose:
                import traceback
                traceback.print_exc()

        # Wait for next interval
        if not _shutdown_requested:
            logger.debug(f"Sleeping for {interval} seconds...")
            try:
                # Use asyncio.sleep with small intervals to check shutdown flag
                for _ in range(interval):
                    if _shutdown_requested:
                        break
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                break

    # Print summary
    total_elapsed = (datetime.now() - start_time).total_seconds()

    logger.info("")
    logger.info("=" * 60)
    logger.info("Collection Summary")
    logger.info("-" * 60)
    logger.info(f"Total runs: {collection_count}")
    logger.info(f"Total snapshots: {total_snapshots:,}")
    logger.info(f"Total time: {total_elapsed:.1f} seconds")
    if collection_count > 0:
        logger.info(f"Avg snapshots/run: {total_snapshots / collection_count:.1f}")
    logger.info("=" * 60)


async def run_single_collection(
    batch_size: int = 20,
    verbose: bool = False,
) -> int:
    """Run a single price collection.

    Args:
        batch_size: Markets to process per batch.
        verbose: Enable verbose logging.

    Returns:
        Number of snapshots collected.
    """
    setup_logging(verbose)

    logger.info("=" * 60)
    logger.info("Polymarket Price Collection (Single Run)")
    logger.info("=" * 60)
    logger.info("")

    start_time = datetime.now()

    collected = await collect_prices_once(
        batch_size=batch_size,
        verbose=verbose,
    )

    elapsed = (datetime.now() - start_time).total_seconds()

    logger.info("")
    logger.info("-" * 60)
    logger.info(f"Completed in {elapsed:.2f} seconds")
    logger.info(f"Snapshots collected: {collected}")
    logger.info("=" * 60)

    return collected


def main() -> None:
    """Main entry point for CLI."""
    parser = argparse.ArgumentParser(
        description="Continuous price collection from Polymarket",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          Run continuous collection (60s interval)
  %(prog)s --interval 30            Collect every 30 seconds
  %(prog)s --once                   Run single collection
  %(prog)s --once --verbose         Single run with debug output
        """,
    )

    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Collection interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=20,
        dest="batch_size",
        help="Markets per batch (default: 20)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit (don't loop)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    # Validate arguments
    if args.interval < 5:
        print("Error: Interval must be at least 5 seconds", file=sys.stderr)
        sys.exit(1)

    if args.batch_size < 1:
        print("Error: Batch size must be at least 1", file=sys.stderr)
        sys.exit(1)

    try:
        if args.once:
            result = asyncio.run(
                run_single_collection(
                    batch_size=args.batch_size,
                    verbose=args.verbose,
                )
            )
            sys.exit(0 if result > 0 else 1)
        else:
            asyncio.run(
                collect_prices_loop(
                    interval=args.interval,
                    batch_size=args.batch_size,
                    verbose=args.verbose,
                )
            )
            sys.exit(0)

    except KeyboardInterrupt:
        # Already handled by signal handler
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
