#!/usr/bin/env python
"""CLI script to backfill historical Polymarket data.

Usage:
    python -m app.scripts.backfill_data --start 2024-01-01 --end 2024-06-01 [OPTIONS]

Options:
    --start         Start date (required, format: YYYY-MM-DD)
    --end           End date (required, format: YYYY-MM-DD)
    --markets       Comma-separated list of market IDs (optional)
    --trades        Backfill trade history
    --prices        Backfill price snapshots (requires existing trades)
    --all           Backfill both trades and prices
    --verbose       Enable verbose logging

Examples:
    # Backfill trades for last 30 days
    python -m app.scripts.backfill_data --start 2024-11-01 --end 2024-12-01 --trades

    # Backfill specific markets
    python -m app.scripts.backfill_data --start 2024-01-01 --end 2024-06-01 \\
        --markets "abc123,def456" --all

    # Backfill everything with verbose output
    python -m app.scripts.backfill_data --start 2024-01-01 --end 2024-12-01 --all -v
"""
import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta
from typing import Optional

# Add parent to path for imports when running as script
if __name__ == "__main__":
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.database import get_session_context
from app.services.data_collector import DataCollector


logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False) -> None:
    """Configure logging for CLI output."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def parse_date(date_str: str) -> datetime:
    """Parse date string in various formats.

    Args:
        date_str: Date string (YYYY-MM-DD, YYYY/MM/DD, etc.)

    Returns:
        Parsed datetime.

    Raises:
        ValueError: If date cannot be parsed.
    """
    formats = [
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue

    raise ValueError(f"Cannot parse date: {date_str}. Use format YYYY-MM-DD")


async def backfill_data(
    start_date: datetime,
    end_date: datetime,
    market_ids: Optional[list[str]] = None,
    include_trades: bool = True,
    include_prices: bool = False,
    verbose: bool = False,
) -> dict[str, int]:
    """Backfill historical data from Polymarket.

    Args:
        start_date: Start of backfill period.
        end_date: End of backfill period.
        market_ids: Optional specific markets to backfill.
        include_trades: Backfill trade history.
        include_prices: Backfill price data from trades.
        verbose: Enable verbose logging.

    Returns:
        Dict with counts of records backfilled.
    """
    setup_logging(verbose)

    logger.info("=" * 60)
    logger.info("Polymarket Data Backfill")
    logger.info("=" * 60)
    logger.info(f"Period: {start_date.date()} to {end_date.date()}")
    logger.info(f"Duration: {(end_date - start_date).days} days")
    if market_ids:
        logger.info(f"Markets: {len(market_ids)} specified")
    else:
        logger.info("Markets: All")
    logger.info(f"Include trades: {include_trades}")
    logger.info(f"Include prices: {include_prices}")
    logger.info("")

    start_time = datetime.now()
    results = {"trades": 0, "prices": 0}

    async with get_session_context() as session:
        collector = DataCollector(session)

        try:
            if include_trades:
                logger.info("-" * 40)
                logger.info("Backfilling trade history...")
                logger.info("-" * 40)

                trades_count = await collector.backfill_historical_data(
                    start=start_date,
                    end=end_date,
                    market_ids=market_ids,
                )
                results["trades"] = trades_count
                logger.info(f"Backfilled {trades_count:,} trade records")

            if include_prices:
                logger.info("")
                logger.info("-" * 40)
                logger.info("Generating price snapshots from trades...")
                logger.info("-" * 40)

                # Generate price snapshots from trade data
                prices_count = await generate_price_snapshots(
                    session=session,
                    start_date=start_date,
                    end_date=end_date,
                    market_ids=market_ids,
                    interval_minutes=5,
                )
                results["prices"] = prices_count
                logger.info(f"Generated {prices_count:,} price snapshots")

        finally:
            await collector.close()

    elapsed = (datetime.now() - start_time).total_seconds()

    logger.info("")
    logger.info("=" * 60)
    logger.info("Summary")
    logger.info("-" * 60)
    logger.info(f"Duration: {elapsed:.2f} seconds")
    logger.info(f"Trades backfilled: {results['trades']:,}")
    logger.info(f"Price snapshots: {results['prices']:,}")
    logger.info("=" * 60)

    return results


async def generate_price_snapshots(
    session,
    start_date: datetime,
    end_date: datetime,
    market_ids: Optional[list[str]] = None,
    interval_minutes: int = 5,
) -> int:
    """Generate price snapshots from trade history.

    Aggregates trade data into OHLCV-style price snapshots.

    Args:
        session: Database session.
        start_date: Start of period.
        end_date: End of period.
        market_ids: Optional market filter.
        interval_minutes: Minutes between snapshots.

    Returns:
        Number of snapshots generated.
    """
    from sqlalchemy import select, func, and_
    from app.models.trade_history import TradeHistory, TradeOutcome
    from app.models.price_history import PriceHistory
    from app.models.market import Market

    # Get markets to process
    if market_ids:
        query = select(Market).where(Market.condition_id.in_(market_ids))
    else:
        query = select(Market)

    result = await session.execute(query)
    markets = result.scalars().all()

    total_snapshots = 0
    interval = timedelta(minutes=interval_minutes)

    for market in markets:
        # Get trades for this market in the time range
        trade_query = (
            select(TradeHistory)
            .where(
                and_(
                    TradeHistory.market_id == market.condition_id,
                    TradeHistory.timestamp >= start_date,
                    TradeHistory.timestamp <= end_date,
                )
            )
            .order_by(TradeHistory.timestamp)
        )

        result = await session.execute(trade_query)
        trades = result.scalars().all()

        if not trades:
            continue

        # Group trades into intervals
        current_interval_start = trades[0].timestamp.replace(
            minute=(trades[0].timestamp.minute // interval_minutes) * interval_minutes,
            second=0,
            microsecond=0,
        )

        interval_trades = []
        snapshots_for_market = 0

        for trade in trades:
            trade_interval = trade.timestamp.replace(
                minute=(trade.timestamp.minute // interval_minutes) * interval_minutes,
                second=0,
                microsecond=0,
            )

            if trade_interval > current_interval_start:
                # Save snapshot for completed interval
                if interval_trades:
                    snapshot = create_snapshot_from_trades(
                        market_id=market.condition_id,
                        timestamp=current_interval_start,
                        trades=interval_trades,
                    )
                    session.add(snapshot)
                    snapshots_for_market += 1

                current_interval_start = trade_interval
                interval_trades = []

            interval_trades.append(trade)

        # Save final interval
        if interval_trades:
            snapshot = create_snapshot_from_trades(
                market_id=market.condition_id,
                timestamp=current_interval_start,
                trades=interval_trades,
            )
            session.add(snapshot)
            snapshots_for_market += 1

        total_snapshots += snapshots_for_market

        # Commit periodically
        if total_snapshots % 1000 == 0:
            await session.commit()
            logger.debug(f"Progress: {total_snapshots:,} snapshots generated")

    await session.commit()
    return total_snapshots


def create_snapshot_from_trades(
    market_id: str,
    timestamp: datetime,
    trades: list,
) -> "PriceHistory":
    """Create a price snapshot from a list of trades.

    Args:
        market_id: Market condition ID.
        timestamp: Snapshot timestamp.
        trades: List of TradeHistory records.

    Returns:
        PriceHistory record.
    """
    from app.models.price_history import PriceHistory
    from app.models.trade_history import TradeOutcome

    # Separate YES and NO trades
    yes_trades = [t for t in trades if t.outcome == TradeOutcome.YES]
    no_trades = [t for t in trades if t.outcome == TradeOutcome.NO]

    # Calculate VWAP for YES
    yes_price = 0.5
    if yes_trades:
        total_value = sum(t.price * t.size for t in yes_trades)
        total_size = sum(t.size for t in yes_trades)
        if total_size > 0:
            yes_price = total_value / total_size

    # Calculate VWAP for NO
    no_price = 1 - yes_price
    if no_trades:
        total_value = sum(t.price * t.size for t in no_trades)
        total_size = sum(t.size for t in no_trades)
        if total_size > 0:
            no_price = total_value / total_size

    # Total volume
    volume = sum(t.size for t in trades)

    return PriceHistory(
        market_id=market_id,
        timestamp=timestamp,
        yes_price=yes_price,
        no_price=no_price,
        volume=volume,
    )


def main() -> None:
    """Main entry point for CLI."""
    parser = argparse.ArgumentParser(
        description="Backfill historical Polymarket data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --start 2024-01-01 --end 2024-06-01 --trades
  %(prog)s --start 2024-01-01 --end 2024-12-01 --all
  %(prog)s --start 2024-06-01 --end 2024-07-01 --markets "abc,def" --trades
        """,
    )

    parser.add_argument(
        "--start",
        required=True,
        type=str,
        help="Start date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end",
        required=True,
        type=str,
        help="End date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--markets",
        type=str,
        default=None,
        help="Comma-separated list of market IDs",
    )
    parser.add_argument(
        "--trades",
        action="store_true",
        help="Backfill trade history",
    )
    parser.add_argument(
        "--prices",
        action="store_true",
        help="Generate price snapshots from trades",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="include_all",
        help="Backfill both trades and prices",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    # Parse dates
    try:
        start_date = parse_date(args.start)
        end_date = parse_date(args.end)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Validate dates
    if end_date <= start_date:
        print("Error: End date must be after start date", file=sys.stderr)
        sys.exit(1)

    if end_date > datetime.now():
        print("Warning: End date is in the future, using current time", file=sys.stderr)
        end_date = datetime.now()

    # Parse market IDs
    market_ids = None
    if args.markets:
        market_ids = [m.strip() for m in args.markets.split(",") if m.strip()]

    # Determine what to backfill
    include_trades = args.trades or args.include_all
    include_prices = args.prices or args.include_all

    if not include_trades and not include_prices:
        print("Error: Must specify --trades, --prices, or --all", file=sys.stderr)
        sys.exit(1)

    try:
        results = asyncio.run(
            backfill_data(
                start_date=start_date,
                end_date=end_date,
                market_ids=market_ids,
                include_trades=include_trades,
                include_prices=include_prices,
                verbose=args.verbose,
            )
        )

        total = results.get("trades", 0) + results.get("prices", 0)
        sys.exit(0 if total > 0 else 1)

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
