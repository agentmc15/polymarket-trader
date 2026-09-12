#!/usr/bin/env python
"""Live probe: how many currently-open markets, per venue, would the
market-making policy actually quote (mm-proveout T7, PLAN.md D1/D8)?

Usage:
    python3 -m app.scripts.probe_quotable

Lists every currently `"open"` market on both venues (the same candidate
set `app.scripts.collect_prices.collect_books_once` hands to
`DataCollector.collect_books`) and prints, per venue, the four counts
`app.services.data_collector.select_quotable_markets` partitions
candidates into:

    listed     -- candidates fetched from the venue's listing.
    two_sided  -- `app.venues.types.quotable_spread` parsed a spread
                  (both sides present, numeric, `0 < bid < ask < 1`),
                  read from the LISTING payload, never an extra
                  `get_book` call.
    quotable   -- of those, spread `>= settings.book_collection_min_spread`
                  AND `venue_volume >= settings.book_collection_min_volume`.
    selected   -- `quotable`, capped at `settings.book_collection_top_n`.

This is the PLAN.md D8 live-payload check for T7's selection change:
`quotable_spread` reading a stale/renamed field name -- or a fixture
that quietly agreed with the code while the live venue did not -- shows
up here as `quotable=0` on a venue that plainly should not be zero.

TWO DISTINCT FAILURES, not one (mm-proveout T7 retry). `selected <
quotable` is normal and must stay quiet -- a venue can legitimately
select fewer than it has quotable via `book_collection_top_n`, and that
alone is not a defect. But `selected == 0` while `quotable > 0` is never
normal: it means the top-N cap ate every quotable market, which happens
silently for a non-positive `book_collection_top_n` (Python's negative
slice semantics turn `top_n=-500` into "drop the last 500", not an
error -- `app.config.Settings` now rejects `<= 0` at construction, but a
`>= 1` cap that is merely too small relative to `quotable` for the
intended selection, or any future bug downstream of `quotable`, would
reach exactly this same shape). The original version of this probe keyed
its exit code ONLY on `quotable`, computed BEFORE the cap is applied --
so a misconfigured cap printed a healthy-looking `quotable=5 selected=0`
and exited `0` anyway, invisible to the one health check meant to catch
exactly this. The exit code below now fails on EITHER `quotable == 0`
OR (`quotable > 0` AND `selected == 0`).

READ-ONLY: `get_market_data_adapters()` returns each venue's READ
adapter (`app.venues.registry.get_read_adapter`, never `get_adapter` --
GUARDRAILS.md §1.1), and this script calls only `list_markets`. No order
is placed, modified, or cancelled, and `TRADING_MODE` is never touched.

Exits `0` if every venue reports `quotable > 0` AND (`selected > 0` OR
`quotable == 0`); `1` otherwise.
"""
from __future__ import annotations

import asyncio
import sys

from app.api.deps import get_market_data_adapters
from app.services.data_collector import select_quotable_markets
from app.venues.base import VenueError
from app.venues.types import VenueId

#: `venue -> (listed, two_sided, quotable, selected)`.
QuotableCounts = dict[VenueId, tuple[int, int, int, int]]


async def probe() -> QuotableCounts:
    """Fetch every open market per venue and partition it by quotability.

    A venue whose `list_markets` call fails (`VenueError`) is reported
    as `(0, 0, 0, 0)` rather than raising or aborting the other venue --
    the same per-venue isolation `DataCollector.collect_books` and
    `app.scripts.collect_prices.collect_books_once` use.

    Returns:
        QuotableCounts: One `(listed, two_sided, quotable, selected)`
            tuple per venue returned by `get_market_data_adapters`.
    """
    adapters = await get_market_data_adapters()
    counts: QuotableCounts = {}
    for venue, adapter in adapters.items():
        try:
            markets = await adapter.list_markets(status="open")
        except VenueError as exc:
            print(f"{venue}: list_markets failed: {exc}", file=sys.stderr)
            counts[venue] = (0, 0, 0, 0)
            continue
        two_sided, quotable, selected = select_quotable_markets(markets)
        counts[venue] = (len(markets), len(two_sided), len(quotable), len(selected))
    return counts


def _render(counts: QuotableCounts) -> str:
    """Render the per-venue counts as a fixed-width table."""
    header = f"{'venue':<12}{'listed':>10}{'two_sided':>12}{'quotable':>10}{'selected':>10}"
    lines = [header]
    for venue in sorted(counts):
        listed, two_sided, quotable, selected = counts[venue]
        lines.append(
            f"{venue:<12}{listed:>10}{two_sided:>12}{quotable:>10}{selected:>10}"
        )
    return "\n".join(lines)


def _health_check_failures(counts: QuotableCounts) -> list[str]:
    """Return one failure message per venue that fails a health check.

    Two DISTINCT checks (mm-proveout T7 retry), not one:

      - `quotable == 0` -- the pre-existing check. Nothing on this venue
        cleared the spread/volume floors at all; most likely
        `quotable_spread`/`venue_volume` reading a stale/renamed field
        against today's live payload.
      - `quotable > 0 and selected == 0` -- the gap this retry closes.
        Something cleared the floors, but the top-N cap
        (`settings.book_collection_top_n`) reduced the selection to
        NOTHING. A `<= 0` `book_collection_top_n` is now rejected at
        `Settings` construction (see `app.config.Settings`'s
        `_reject_non_positive_top_n`), but this check stays regardless
        -- it is the only thing standing between a future regression in
        that guard, or a `>= 1` cap that is simply wrong, and an
        indefinite, silent collection blackout on an otherwise healthy
        venue.

    `selected == 0` on its own is NOT a failure when `quotable == 0`
    too -- that shape is already covered by the first check and would
    otherwise double-report the same venue.

    Args:
        counts: `venue -> (listed, two_sided, quotable, selected)`, as
            returned by `probe()`.

    Returns:
        list[str]: One human-readable failure line per failing venue,
            for `venue`s sorted for stable output. Empty when every
            venue is healthy.
    """
    failures: list[str] = []
    for venue in sorted(counts):
        _, _, quotable, selected = counts[venue]
        if quotable == 0:
            failures.append(
                f"{venue}: quotable=0 -- collection would run and write "
                "nothing for this venue. Before assuming the market is "
                "simply this tight venue-wide, check quotable_spread's "
                "field names (app.venues.types: Kalshi "
                "yes_bid_dollars/yes_ask_dollars, Polymarket "
                "bestBid/bestAsk) against today's live payload."
            )
        elif selected == 0:
            failures.append(
                f"{venue}: quotable={quotable} but selected=0 -- the "
                "book_collection_top_n cap ate every quotable market on "
                "this venue. Collection would run and write nothing here "
                "even though the market is healthy. Check "
                "settings.book_collection_top_n (must be a positive "
                "integer large enough for the intended selection -- "
                "app.config.Settings rejects <= 0, but a small positive "
                "value can still zero this out)."
            )
    return failures


def main() -> int:
    """Run the probe against the live venues and print the report.

    Returns:
        int: `0` if every venue is healthy (see `_health_check_failures`),
            else `1`.
    """
    counts = asyncio.run(probe())
    print(_render(counts))

    failures = _health_check_failures(counts)
    if failures:
        print("\nFAIL:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
