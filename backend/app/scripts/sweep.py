#!/usr/bin/env python
"""CLI for the capital sweep / `EdgeDecayReport` (PLAN.md D12, T22).

Usage:
    python -m app.scripts.sweep --synthetic [--strategy NAME] [--levels ...] [--out PATH]
    python -m app.scripts.sweep --strategy NAME --start ISO --end ISO [--levels ...] [--out PATH]

Examples:
    # Demonstrable without a database: a planted complement gap on
    # `create_sample_snapshots` (PLAN.md D6 depth, T22 carry-forward 3 --
    # the un-planted fixture is deliberately arbitrage-neutral).
    python -m app.scripts.sweep --synthetic --levels 500,5000,50000 --out /tmp/sweep.json

    # A real, DB-backed sweep.
    python -m app.scripts.sweep --strategy binary_complement_arbitrage \\
        --start 2024-01-01T00:00:00Z --end 2024-06-01T00:00:00Z \\
        --levels 500,2000,10000,50000,250000 --out /tmp/sweep.json

GUARDRAILS.md §1.7 / PLAN.md D12: `depth_source`/`fill_at` are printed on
every row, and `sweep_ceiling_note` (and `unmeasurable_note`, when set)
are PRINTED, not just written to the JSON -- "the sweep ceiling is not
proof" is a caveat that must be seen, not merely stored.
"""
import argparse
import asyncio
import json
import logging
import random
import sys
from dataclasses import replace as dataclasses_replace
from datetime import UTC, datetime, timedelta
from typing import Any

# Add parent to path for imports when running as script
if __name__ == "__main__":
    import os
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )

from app.services.backtesting import (
    BacktestConfig,
    EdgeDecayReport,
    edge_decay_report_to_dict,
    run_sweep,
)
from app.services.backtesting.data_replay import DataReplayer, create_sample_snapshots
from app.strategies import STRATEGIES
from app.strategies.base import MarketSnapshot

logger = logging.getLogger(__name__)

#: Default gross gap planted onto every synthetic snapshot's
#: `yes_ask + no_ask` for `--synthetic` mode (T22 carry-forward 3: the
#: un-planted `create_sample_snapshots` fixture is deliberately
#: arbitrage-NEUTRAL -- `no_price = 1 - yes_price` and a strictly
#: positive spread means `yes_ask + no_ask > 1` always).
_DEFAULT_SYNTHETIC_GAP = 0.06

#: 24h notional (USD) forced onto every `--synthetic` snapshot, which is
#: what sets the fixture's synthesized book depth (T28, PLAN.md R4).
#:
#: `create_sample_snapshots` draws `volume_24h ~ U(10_000, 100_000)`, and
#: `synthesize_book` rests `settings.liquidity_fraction * volume_24h /
#: price` contracts at the touch -- 222 to 197,794 contracts on a
#: measured 30-day run, median 2,322. `binary_complement_arbitrage`
#: orders a FIXED `min_position_size = 100` contracts per leg at EVERY
#: capital level, so the order was at worst 45% of the thinnest level in
#: the entire fixture and the book could never run out. That is exactly
#: R4's "the fixture is too deep", and it is why the sweep's depth signal
#: was 0.0% at $50k and $250k: not because deep capital fills cleanly,
#: but because nothing in the fixture could ever bind.
#:
#: 4,000 puts the synthesized level at `0.02 * 4000 / price` = 80
#: contracts at `price = 0.50`, straddling the 100-contract order so the
#: book binds on some snapshots and not others. It stays above
#: `binary_complement_arbitrage`'s `min_liquidity` (1,000), so the
#: strategy still signals. Measured across seeds 0-9 the depth signal at
#: $250k lands between 9.3% and 20.4% while the capital-cap signal is
#: 0.0% at every seed -- the two series provably run in opposite
#: directions.
_DEFAULT_SYNTHETIC_VOLUME_24H = 4_000.0

#: Seed for the `--synthetic` random walk. `create_sample_snapshots`
#: draws from the global `random` module, so the demo was previously a
#: different fixture on every run -- and a tripwire measured on a
#: different fixture each time is not a tripwire. Seeded by default (and
#: the global RNG state is restored afterwards, so seeding the demo does
#: not reseed the calling process). Pass `--synthetic-seed -1` for the
#: old unseeded behavior.
_DEFAULT_SYNTHETIC_SEED = 0


def setup_logging(verbose: bool = False) -> None:
    """Configure logging for CLI output."""
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _parse_levels(raw: str | None) -> list[float] | None:
    """Parse `--levels 500,2000,10000` into `[500.0, 2000.0, 10000.0]`.

    Args:
        raw: Comma-separated levels, or `None`.

    Returns:
        list[float] | None: Parsed levels, or `None` to use
            `DEFAULT_CAPITAL_LEVELS`.

    Raises:
        ValueError: If any entry is not a valid float, or the parsed
            list is empty.
    """
    if raw is None:
        return None
    levels = [float(part.strip()) for part in raw.split(",") if part.strip()]
    if not levels:
        raise ValueError("--levels parsed to an empty list")
    return levels


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 datetime, defaulting a naive result to UTC.

    Args:
        value: ISO-8601 datetime string.

    Returns:
        datetime: Aware UTC datetime.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def plant_complement_gap(
    snapshots: list[MarketSnapshot], *, gap: float = _DEFAULT_SYNTHETIC_GAP
) -> list[MarketSnapshot]:
    """Shave `gap` off every snapshot's `yes_ask + no_ask` sum.

    `create_sample_snapshots` is deliberately arbitrage-neutral (T22
    carry-forward 3): it sets `no_price = 1 - yes_price` and a strictly
    positive spread, so `yes_ask + no_ask = 1 + spread > 1` on every
    snapshot it produces. This function plants a REAL complement
    violation on top of that random walk by moving each side's ask
    `gap / 2` closer to zero (and its bid a further cent inside that, so
    the resulting book is never crossed), clamped at `0.01` so an
    extreme random-walk price can never go non-positive. The category is
    also set to `"politics"` (a real, known Polymarket taker-rate
    category, 4% -- see `app/venues/fees.py`) so the demo prices with a
    real fee schedule rather than the unknown-category fallback.

    Args:
        snapshots: Snapshots from `create_sample_snapshots`.
        gap: Target `1 - (yes_ask + no_ask)`, before clamping.

    Returns:
        list[MarketSnapshot]: New snapshots (originals untouched) with
            the gap planted.
    """
    shave = gap / 2.0
    planted = []
    for snap in snapshots:
        yes_ask = max(0.01, snap.yes_price - shave)
        no_ask = max(0.01, snap.no_price - shave)
        planted.append(
            dataclasses_replace(
                snap,
                yes_ask=yes_ask,
                yes_bid=max(0.0, yes_ask - 0.01),
                no_ask=no_ask,
                no_bid=max(0.0, no_ask - 0.01),
                category="politics",
            )
        )
    return planted


def build_synthetic_snapshots(
    *,
    market_id: str = "sweep-demo",
    days: int = 30,
    interval_minutes: int = 15,
    gap: float = _DEFAULT_SYNTHETIC_GAP,
    volume_24h: float | None = _DEFAULT_SYNTHETIC_VOLUME_24H,
    seed: int | None = _DEFAULT_SYNTHETIC_SEED,
) -> list[MarketSnapshot]:
    """Build a planted-gap synthetic fixture for `--synthetic` mode.

    Args:
        market_id: Synthetic market id.
        days: Length of the window, days.
        interval_minutes: Minutes between snapshots.
        gap: Planted `yes_ask + no_ask` gap -- see `plant_complement_gap`.
        volume_24h: 24h notional (USD) forced onto every snapshot, which
            is what sets the synthesized book's depth. `None` keeps
            `create_sample_snapshots`'s own `U(10_000, 100_000)` draw,
            which is too deep for any capital level to exhaust -- see
            `_DEFAULT_SYNTHETIC_VOLUME_24H` for the measurement.
        seed: Seed for the random walk, or `None` to leave the global
            RNG alone. The global `random` state is saved and restored
            around the seeded draw, so this never reseeds the caller.

    Returns:
        list[MarketSnapshot]: Aware-UTC snapshots with a planted
            complement gap, `depth_source="synthetic"` once walked
            (PLAN.md D6 -- no recorded book is attached here).
    """
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    state = random.getstate()
    try:
        if seed is not None:
            random.seed(seed)
        base = create_sample_snapshots(
            market_id=market_id,
            start_date=start,
            end_date=end,
            interval_minutes=interval_minutes,
        )
    finally:
        random.setstate(state)
    if volume_24h is not None:
        base = [
            dataclasses_replace(snap, volume_24h=volume_24h) for snap in base
        ]
    return plant_complement_gap(base, gap=gap)


def print_report(report: EdgeDecayReport, *, strategy_name: str) -> None:
    """Print the sweep as a table, with every trustworthiness label.

    GUARDRAILS.md §1.7 / PLAN.md D12: `depth_source`/`fill_at` are
    printed on EVERY row (not just once), and the ceiling caveat is
    printed, never left to live only in the JSON output.

    Args:
        report: The completed sweep report.
        strategy_name: Name of the strategy that was swept, for the
            header.
    """
    print(f"\nCapital sweep: {strategy_name}")
    print("=" * 100)
    header = (
        f"{'capital':>12} | {'net_return':>10} | {'annualized':>10} | "
        f"{'trades':>6} | {'fill_rate':>9} | {'cap_capped':>10} | "
        f"{'depth_ltd':>9} | {'downsized':>9} | "
        f"{'slip(bps)':>9} | {'util':>6} | {'depth_source':>12} | "
        f"{'fill_at':>7} | {'unvalidated':>11} | {'unmarked':>8}"
    )
    print(header)
    print("-" * len(header))
    for row in report.rows:
        cause = f"  [{row.zero_trades_cause}]" if row.zero_trades_cause else ""
        print(
            f"{row.capital:>12,.0f} | {row.net_return:>10.2%} | "
            f"{row.annualized:>10.2%} | {row.trades:>6} | "
            f"{row.fill_rate:>9.1%} | {row.pct_intents_capital_capped:>10.1%} | "
            f"{row.pct_intents_depth_limited:>9.1%} | "
            f"{row.pct_intents_downsized:>9.1%} | "
            f"{row.avg_slippage_bps:>9.1f} | {row.capital_utilization:>6.1%} | "
            f"{row.depth_source:>12} | {row.fill_at:>7} | "
            f"{row.tick_unvalidated_fills:>11} | {len(row.unmarked_positions):>8}"
            f"{cause}"
        )
    print("-" * len(header))
    # T28: the three shortfall columns are not interchangeable, and the
    # legend says so on every run rather than living only in a docstring.
    print(
        "cap_capped = share of sized intents the engine's own "
        "max_position_pct/cash cap scaled down (falls with capital)."
    )
    print(
        "depth_ltd  = share of sized intents where a leg's walk consumed "
        "the book and came up short (PLAN.md R4's tripwire; rises with "
        "capital when order size does)."
    )
    print(
        "downsized  = the UNION of the two, over executed trackable "
        "intents only. Kept for continuity; it is NOT the depth signal."
    )
    blocked = sum(row.depth_blocked_intents for row in report.rows)
    if blocked:
        print(
            f"Of the depth-limited intents, {blocked} across all levels "
            "committed NOTHING (an all_or_none intent killed by a short "
            "leg) -- those also depress fill_rate; the two are not summed."
        )
    if report.edge_dies_at is not None:
        print(f"edge_dies_at: ${report.edge_dies_at:,.0f}")
    else:
        print("edge_dies_at: None (edge survived every level tested)")
    print(f"depth_source (aggregate): {report.depth_source}")
    print(f"fill_at: {report.fill_at}")
    print()
    # PLAN.md D12: printed, not merely stored.
    print(f"NOTE: {report.sweep_ceiling_note}")
    if report.unmeasurable_note is not None:
        print(f"WARNING: {report.unmeasurable_note}")
    print()


async def run_synthetic(
    *,
    strategy_name: str,
    strategy_config: dict[str, Any],
    levels: list[float] | None,
    days: int,
    interval_minutes: int,
    gap: float,
    volume_24h: float | None,
    seed: int | None,
) -> EdgeDecayReport:
    """Run a sweep on a planted-gap synthetic fixture (no database).

    Args:
        strategy_name: Registry name to sweep.
        strategy_config: Strategy configuration overrides.
        levels: Capital levels, or `None` for `DEFAULT_CAPITAL_LEVELS`.
        days: Synthetic fixture window, days.
        interval_minutes: Synthetic fixture snapshot interval.
        gap: Planted complement gap.
        volume_24h: 24h notional forced onto every snapshot (sets the
            synthesized book depth), or `None` for the random draw.
        seed: Random-walk seed, or `None` to leave the RNG alone.

    Returns:
        EdgeDecayReport: The completed sweep.
    """
    snapshots = build_synthetic_snapshots(
        days=days,
        interval_minutes=interval_minutes,
        gap=gap,
        volume_24h=volume_24h,
        seed=seed,
    )
    start_date = snapshots[0].timestamp - timedelta(minutes=1)
    end_date = snapshots[-1].timestamp + timedelta(minutes=1)
    base_config = BacktestConfig(start_date=start_date, end_date=end_date)

    def factory() -> Any:
        from app.services.backtesting.data_replay import InMemoryDataReplayer

        return InMemoryDataReplayer(snapshots=snapshots)

    return await run_sweep(strategy_name, strategy_config, base_config, factory, levels)


async def run_db_backed(
    *,
    strategy_name: str,
    strategy_config: dict[str, Any],
    start: datetime,
    end: datetime,
    fill_at: str,
    levels: list[float] | None,
) -> EdgeDecayReport:
    """Run a sweep against recorded database history.

    Args:
        strategy_name: Registry name to sweep.
        strategy_config: Strategy configuration overrides.
        start: Replay window start, aware UTC.
        end: Replay window end, aware UTC.
        fill_at: `"same"` or `"next"` (PLAN.md D6; `"same"` is a
            diagnostic only -- see `BacktestConfig.fill_at`).
        levels: Capital levels, or `None` for `DEFAULT_CAPITAL_LEVELS`.

    Returns:
        EdgeDecayReport: The completed sweep.
    """
    from app.database import get_session_context

    base_config = BacktestConfig(start_date=start, end_date=end, fill_at=fill_at)  # type: ignore[arg-type]

    async with get_session_context() as session:

        def factory() -> Any:
            return DataReplayer(session=session, start_date=start, end_date=end)

        return await run_sweep(strategy_name, strategy_config, base_config, factory, levels)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Capital sweep / EdgeDecayReport (PLAN.md D12, T22)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--strategy",
        default="binary_complement_arbitrage",
        choices=sorted(STRATEGIES.keys()),
        help="Strategy to sweep (default: binary_complement_arbitrage)",
    )
    parser.add_argument(
        "--strategy-config",
        default=None,
        help="JSON object of strategy config overrides",
    )
    parser.add_argument("--start", default=None, help="ISO-8601 start date (DB mode)")
    parser.add_argument("--end", default=None, help="ISO-8601 end date (DB mode)")
    parser.add_argument(
        "--fill-at",
        default="next",
        choices=["same", "next"],
        help="Fill timing (default: next; 'same' is a DIAGNOSTIC only, PLAN.md D6)",
    )
    parser.add_argument(
        "--levels",
        default=None,
        help="Comma-separated capital levels, e.g. 500,2000,10000 "
        "(default: 500,2000,10000,50000,250000)",
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Run on a planted-gap synthetic fixture -- no database needed (T22)",
    )
    parser.add_argument(
        "--synthetic-days",
        type=int,
        default=30,
        help="Synthetic fixture window, days (default: 30)",
    )
    parser.add_argument(
        "--synthetic-interval-minutes",
        type=int,
        default=15,
        help="Synthetic fixture snapshot interval, minutes (default: 15)",
    )
    parser.add_argument(
        "--gap",
        type=float,
        default=_DEFAULT_SYNTHETIC_GAP,
        help=f"Planted complement gap for --synthetic (default: {_DEFAULT_SYNTHETIC_GAP})",
    )
    parser.add_argument(
        "--synthetic-volume-24h",
        type=float,
        default=_DEFAULT_SYNTHETIC_VOLUME_24H,
        help=(
            "24h notional (USD) on every synthetic snapshot -- this sets "
            "the synthesized book depth (liquidity_fraction * volume_24h "
            f"/ price contracts). Default: {_DEFAULT_SYNTHETIC_VOLUME_24H:,.0f}. "
            "Negative keeps create_sample_snapshots' own U(10k, 100k) "
            "draw, which is too deep for any capital level to exhaust "
            "(PLAN.md R4)"
        ),
    )
    parser.add_argument(
        "--synthetic-seed",
        type=int,
        default=_DEFAULT_SYNTHETIC_SEED,
        help=(
            "Seed for the --synthetic random walk (default: "
            f"{_DEFAULT_SYNTHETIC_SEED}); -1 leaves the RNG unseeded, so "
            "every run is a different fixture"
        ),
    )
    parser.add_argument("--out", default=None, help="Write the JSON report to this path")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")

    args = parser.parse_args()
    setup_logging(args.verbose)

    try:
        levels = _parse_levels(args.levels)
        strategy_config: dict[str, Any] = (
            json.loads(args.strategy_config) if args.strategy_config else {}
        )

        if args.synthetic:
            report = asyncio.run(
                run_synthetic(
                    strategy_name=args.strategy,
                    strategy_config=strategy_config,
                    levels=levels,
                    days=args.synthetic_days,
                    interval_minutes=args.synthetic_interval_minutes,
                    gap=args.gap,
                    volume_24h=(
                        None
                        if args.synthetic_volume_24h < 0
                        else args.synthetic_volume_24h
                    ),
                    seed=None if args.synthetic_seed < 0 else args.synthetic_seed,
                )
            )
        else:
            if not args.start or not args.end:
                print(
                    "Error: --start and --end are required unless --synthetic is set",
                    file=sys.stderr,
                )
                sys.exit(2)
            report = asyncio.run(
                run_db_backed(
                    strategy_name=args.strategy,
                    strategy_config=strategy_config,
                    start=_parse_iso(args.start),
                    end=_parse_iso(args.end),
                    fill_at=args.fill_at,
                    levels=levels,
                )
            )
    except Exception as exc:  # noqa: BLE001 - CLI top-level error boundary
        print(f"Error: {exc}", file=sys.stderr)
        if args.verbose:
            import traceback

            traceback.print_exc()
        sys.exit(1)

    print_report(report, strategy_name=args.strategy)

    if args.out:
        payload = edge_decay_report_to_dict(report)
        with open(args.out, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        print(f"Wrote JSON report to {args.out}")

    sys.exit(0)


if __name__ == "__main__":
    main()
