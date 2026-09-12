#!/usr/bin/env python
"""Replay `MarketMaker` over collected `book_snapshots`, both venues, T2's report.

WHY THIS EXISTS (mm-proveout T11, PLAN.md D2/D11). Kalshi can be backtested
retrospectively because it publishes candlesticks (`app.scripts.mm_backtest`,
T2). Polymarket cannot -- "no historical bid/ask, no public tape" (PLAN.md
verified facts) -- so the only evidence it can ever produce is the forward
`book_snapshots` collection Phase 2/2b built and gated. This module turns
those rows into the SAME `(quote, fill, mark)` triples T2 replays, and hands
them to T2's OWN `replay()`/`report()` -- imported, never reimplemented
(PLAN.md D11: two report implementations would drift exactly as the two
volume helpers did). `tests/scripts/test_mm_replay_snapshots.py` asserts this
module's `report`/`replay` names ARE `app.scripts.mm_backtest`'s, by identity
of the imported callable -- not merely "produces the same numbers on this
input".

IT WILL RUN AGAINST AN EMPTY OR NEARLY-EMPTY TABLE TODAY. Phase 2b's
collection beat is only just landing; exiting 1 with a clear count of what
was and was not found is the CORRECT behaviour here, not a failure this
module should paper over.

THE MAPPING, snapshot -> `Candle` (T1's shape, `app.venues.kalshi.candles`).
For one `(venue, market_id, outcome="YES")` series, sorted ascending by `ts`
(never `observed_at` -- see below), snapshot `j`'s book gives candle `j`'s
`bid_close`/`ask_close` directly (its OWN best bid/ask, exactly as a Kalshi
candle's `bid_close`/`ask_close` are its own period-close quote). Candle
`j`'s `px_low`/`px_high`/`volume` describe the INTERVAL ENDING at snapshot
`j` (i.e. between snapshot `j-1` and `j`), which is what `replay()` reads as
`candles[i+1]` for the fill decided while the quote from `candles[i]` rested:

  * `volume` is the DELTA of `volume_lifetime` between snapshot `j-1` and
    `j` -- see "THE CORRECTION" below for why this is `volume_lifetime`, not
    `volume`, and why a `None`/negative delta is excluded, never defaulted.
  * `px_low`/`px_high` are snapshot `j`'s OWN best bid/ask (never a
    lifetime-volume-implied price) -- there is no public trade tape on
    Polymarket, so the closest thing to "what printed while the quote
    rested" is "where the book was when next observed". A pessimistic fill
    (`passive_fill.py`'s `_filled`) therefore requires the NEXT best bid to
    have moved BELOW the quoted bid (a real level a taker crossed through)
    -- `trades.low < quote.price` becomes `best_bid_{j} < quote_bid` -- and
    symmetrically for a sell. An optimistic fill only requires TOUCHING it.

THE CORRECTION (from the Phase 2 review and T15; read before changing
anything above). The brief this module was written from originally said
`volume = (volume_{i+1} - volume_i) if both non-null else 1.0`, reading the
`volume` column. Both halves of that were wrong:

  1. WRONG COLUMN. `volume` is `venue_volume(market)`, which tries the
     24-HOUR fields first (`volume_24h_fp`/`volume24hr`) because it exists
     for RANKING. Measured live 2026-09-07: Kalshi's `volume_24h_fp` did not
     move for a single one of 6,058 actively-traded markets over 245
     seconds -- its delta is always zero, so a replay keyed on it would
     replay to zero fills forever. `volume_lifetime` (T15; Kalshi
     `volume_fp`, Polymarket `volumeNum`) exists for exactly this
     between-snapshot-delta job and is what this module reads.
  2. `else 1.0` FABRICATES EVIDENCE. `volume_lifetime` is `None` when never
     populated, or when T15's write-time monotonicity guard caught a
     restatement (Polymarket's lifetime counter was measured DECREASING for
     29 of 255 markets in ~28 minutes). `None` means UNKNOWN.
     `passive_fill.py:142` returns no fills at all when `volume <= 0.0`, and
     GATES BOTH FILL MODELS on it, not only the optimistic one -- so
     substituting `1.0` for an unknown delta would assert a trade occurred
     and manufacture fills under BOTH models that the evidence does not
     support. This module instead marks such an interval's `px_low`/
     `px_high` as `None` (the same signal T2's own zero-volume Kalshi
     candles already carry -- "no price object, no fill decision possible")
     and counts it separately as `n_unknown_volume_intervals`, reported in
     the header beside `n`, exactly as T2 reports `n_unmarkable_intervals`.
     `_volume_delta` below is the single place this decision is made; see
     its docstring for the (defensive, redundant-on-purpose) negative-delta
     guard too.

`observed_at` (T15), NOT `ts`, FOR REASONING ABOUT DURATION. Sorting and the
quote/fill/mark index arithmetic above all use `ts` -- the book's own
identity, matching T2's "candles ascending by end_ts" convention. But `ts`
is Polymarket's CLOB book-move time, not poll time, and T8's same-`ts`
refresh path means a quiet market's `ts` can sit unchanged for many polls
while `volume`/fee are silently updated in place from the LATEST poll that
saw it (`book_snapshot.py`'s "THE FEE IN FORCE"/"THE SHIFT THIS COLUMN LETS
T11 CORRECT FOR"). So two consecutive DISTINCT `ts` values can be seconds or
days apart, and a row's own `volume_lifetime`/fee reading describes a moment
closer to the NEXT snapshot's `ts` than to this row's. This module reports
`median_observed_dwell_s` -- the median gap between consecutive
`observed_at` values, pooled across every replayed market -- BESIDE the
`ts`-derived data window, and a reader should conclude: when this is far
smaller than the span between `first_ts`/`last_ts`, most of that span is one
or a few long-quiet Polymarket books, not many short Kalshi-style intervals,
and the fill/volume decisions above are still correct (they are keyed on
distinct `ts`, not on `observed_at`) but each one represents a LONGER, less
frequently-confirmed resting period than its `ts` gap alone would suggest.

REUSE, NOT REIMPLEMENTATION (PLAN.md D11). `replay()`/`report()` are
imported from `app.scripts.mm_backtest` unmodified. The one place this
module cannot hand T2's `replay()` a fully faithful call unmodified: T2's
`MarketCandles` carries no `tick_size` field, and `replay()` takes ONE
scalar `tick_size` for its whole call -- fine for Kalshi, where every T2
sample uses 0.01, but not a safe assumption for Polymarket, whose CLOB tick
sizes vary by market (GUARDRAILS.md §3.2: a fixture-plausible-but-live-wrong
field is this repo's most repeated defect). `replay_snapshots()` below
groups markets by their OWN observed tick size and calls `replay()` once per
distinct value, merging the `ReplayResult`s -- never one call with an
assumed constant.

FEES (T8/T15's per-snapshot columns). `passive_fill.PassiveFillEngine` is
built ONCE per market inside `replay()`, against ONE `FeeSchedule` -- it has
no per-interval fee parameter, so a market whose fee changed mid-series
(Polymarket's `feeSchedule` does change over time, per-market) is replayed
under a single representative schedule: the LATEST snapshot row carrying a
COMPLETE reading (`taker_fee_rate`, `maker_fee_rate`, `fee_source` AND
`maker_rebate_rate` all non-null -- see `fee_schedule_for_market`). A market
with no complete reading anywhere in its history falls back to the venue
default (`default_kalshi_schedule()`/`category_fee_schedule(None)`), same as
T2. The maker rebate is carried on that `FeeSchedule` and, exactly as in T2,
never enters `pnl` -- it is `rebate_if_paid_not_in_pnl`, its own reported
line (GUARDRAILS.md §2.3).

TERMINAL INVENTORY settles at the venue's real RESULT, fetched via
`adapter.list_markets(status="resolved")` at analysis time -- never marked
(PLAN.md D4). A market not found in that listing, or found with a `result`
outside `{"yes","no"}`, has no settlement price: it is `unsettled`, excluded
from `MarketCandles`/the verdict, and counted (`n_unsettled`), mirroring
T2's `settled_universe`.

MARKOUT-ONLY MODE (`--markout-only`). Kalshi can be scored on settlement
because 50,704 markets closed in one measured 2026-06-30 -> 2026-09-07
window alone (PLAN.md); Polymarket's OWN measured quotable universe is
130 markets across 53 events at spread >= 0.10 (75 across 32 events at >=
0.25 -- `reports/polymarket-feasibility.md` §1/§2, measured
2026-09-12T15:55Z), and ~90% of that cohort closes on one date around
2026-12-31 -- reaching this kit's power standard on CASH P&L is ~2.4
years at 0.10 and ~4.2 years at 0.25.

**THIS MODE, not the statistic, is what needs no settlement.** An earlier
version of this docstring claimed "`markout_pnl` needs no settlement",
and that is FALSE in the default `terminal=settled` mode: `mm_backtest`
adds `inventory * (settle - last_mid)` for terminal inventory to the
markout accumulator as well as to cash (see `replay()`, the block under
"Terminal inventory settles at the venue's real result"). The two bases
differ in HOW MUCH settlement they absorb -- cash receives the whole
`inventory * settle`, markout only the move from `last_mid` -- not in
whether they absorb any. On the honest Kalshi holdout that term is not a
rounding detail: 805 of 919 trading markets held inventory into
settlement. Only `--markout-only` is genuinely settlement-free, because
`_strip_terminal_settlement` reverses that term and stamps
`terminal="excluded"`. The per-fill component alone is mark-based, which
is what makes the mode possible; the statistic as a whole is not. This mode exists to run
the Kalshi finding that survived every attack -- wide quoting earning
$0.2915/fill against tight quoting's $0.0104 (28x), tight quoting's
per-fill edge going NEGATIVE outside one sport ($-0.0528) -- against
Polymarket's own quotable cohort in weeks, without waiting for a single
market to resolve.

Three consequences follow from "no settlement", and this module enforces
all three rather than trusting a caller to remember them:

  1. UNSETTLED MARKETS ARE INCLUDED, never counted as `n_unsettled` and
     dropped. `load_market_candles(..., markout_only=True)` still needs
     `event_id`/`close_time` for a market with no result yet -- `_main`
     supplies those from `all_markets()` (`list_markets(status=None)`,
     every status) instead of `resolved_markets()` (`status="resolved"`
     only). `MarketCandles.__post_init__` (`app.scripts.mm_backtest`,
     unmodified) still requires `result in SETTLEABLE_RESULTS`, so an
     unsettled market is built with `_MARKOUT_PLACEHOLDER_RESULT`
     standing in for the real one -- arithmetically inert, see point 3.
     A market found in NO listing at all (any status) still cannot be
     built (no `event_id`/`close_time` to build it from) and is counted
     separately (`n_no_market_metadata`), markout-only or not.
  2. CASH P&L IS NEVER A NUMBER. `pnl`'s `terminal_inventory * settle`
     term is genuinely undefined without a real result; reporting it
     computed against a placeholder would be exactly the "collapsed a
     missing measurement into a plausible constant" defect this task was
     written to stop happening a sixth time. `to_markout_only_report()`
     replaces every cash-basis figure `report()` produces (`total_pnl`,
     `mean_pnl_per_trading_market`, `sd_pnl_per_trading_market`, the
     cash-clustered CI, `roc`, the power table's P&L cells, and the whole
     `verdict`, which IS the cash go/no-go) with `None` -- never `0.0` --
     and labels `pnl_basis` with `CASH_PNL_UNAVAILABLE`, a STRING, so a
     caller cannot silently average it into anything.
  3. TERMINAL INVENTORY NEVER ENTERS THE REPORTED MARKOUT STATISTIC.
     `markout_pnl`'s own settlement term, `terminal_inventory * (settle -
     last_mid)`, is exactly as undefined as `pnl`'s -- `settle` needs a
     real result. Rather than strip it only for the markets that happen
     to lack one, which would silently mix terminal-inclusive and
     terminal-excluded markets inside the SAME reported total,
     `replay_snapshots(..., markout_only=True)` strips it from EVERY row
     alike (`_strip_terminal_settlement`): it reverses `replay()`'s own
     arithmetic exactly (the same `candles.settle`, `row.terminal_
     inventory`, `row.last_mid` `replay()` itself read), so a genuinely
     settled market loses only what its terminal inventory actually
     contributed and a placeholder-settled one loses only what the
     placeholder invented. What remains is a PURE per-fill statistic: the
     sum of `mark_to_market(fill, mid(i+2))` over every fill, nothing
     else. READ THIS AS QUOTE QUALITY, NOT MONEY: a reader MAY conclude
     the mechanism (does resting tight vs wide earn or lose against the
     next tick) transfers to this venue's quotable cohort; a reader may
     NOT conclude anything about capital at risk, what a book banked, or
     a go/no-go on money -- none of that survives without a settled
     result.

`terminal=` carries `MARKOUT_ONLY_TERMINAL` ("excluded"), never T2's
`TERMINAL` ("settled") -- GUARDRAILS.md §2.1 requires the label, and
"settled" would misdescribe what point 3 above just stripped out.
`report()` itself always writes `"settled"` (`app.scripts.mm_backtest`'s
own hardcoded module constant; this kit does not touch that file) --
`to_markout_only_report()` is the one place that corrects the label
after the fact, on the SAME dict `report()` returned, never by
recomputing a statistic `report()` already computed.

PER-FILL MARKOUT EDGE is added as `mean_markout_pnl_per_fill` on every
block (`total_markout_pnl / n_fills`, `None` when `n_fills == 0`) beside
the existing per-MARKET figure, because the Kalshi finding this mode
exists to test is stated PER FILL ($0.2915 vs $0.0104), and a cross-venue
comparison of it has to be like-for-like.

REUSE, STILL (PLAN.md D11). `replay()`/`report()` are called exactly as
settled mode calls them -- `markout_only` only changes what gets BUILT
into a `MarketCandles` before the call and what gets read out of
`report()`'s dict after it. `test_report_is_byte_identical_to_t2s_report
_by_identity` still holds; there is exactly one report implementation.

Settled mode (the default, `markout_only=False`) is UNCHANGED by any of
the above -- every existing code path, exclusion count and test keeps its
prior behaviour exactly.

READ-ONLY. The only venue traffic this module makes is `list_markets`
(GUARDRAILS.md §1.1/§1.3/§3.1 -- read-only public GETs are the one
network access this kit lifts): settled mode asks `status="resolved"`
(`resolved_markets`), markout-only mode asks `status=None`, the adapter's
own "every status" value (`all_markets`) -- the SAME method, no new
endpoint. The only database traffic is a `SELECT` over `book_snapshots`.
This module never opens `.env` and has no code path that could place,
modify, or cancel an order.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.execution.passive_fill import FillModel
from app.logging_config import configure_logging
from app.models.book_snapshot import BookSnapshot
from app.scripts.collection_health import _ensure_utc
from app.scripts.mm_backtest import (
    DEFAULT_QUOTE_SIZE,
    DEFAULT_SEED,
    MIN_CANDLES,
    SETTLEABLE_RESULTS,
    MarketCandles,
    MarketRow,
    ReplayResult,
    _cutoff_ts,
    _print_model,
    replay,
    report,
)
from app.strategies.base import outcome_key
from app.strategies.market_making import (
    DEFAULT_EDGE_FRACTION,
    DEFAULT_MAX_INVENTORY,
    DEFAULT_MIN_SPREAD,
    DEFAULT_SKEW_STRENGTH,
    MarketMaker,
)
from app.utils.time import ensure_aware, utcnow
from app.venues.base import FeeModel
from app.venues.fees import (
    KalshiFeeModel,
    PolymarketFeeModel,
    category_fee_schedule,
    default_kalshi_schedule,
)
from app.venues.kalshi.candles import Candle, series_for
from app.venues.types import BookLevel, FeeSchedule, OrderBook, VenueId, VenueMarket

#: Canonical identity for the binary YES outcome (`app.strategies.base.
#: outcome_key`) -- the same identity `_upsert_book_snapshot` canonicalizes
#: every `BookSnapshot.outcome` through before writing it. Both venues'
#: candle replay prices a single binary contract (matching T2's Kalshi
#: `yes_bid`/`yes_ask` convention); a >=3-outcome bundle market's other legs
#: are out of scope for this replay.
_OUTCOME_YES = outcome_key("YES")

#: Venues this replay knows how to price. Forward collection (Phase 2/2b)
#: runs on both.
_VENUES: tuple[VenueId, ...] = ("kalshi", "polymarket")

#: `MarketCandles.result` for an unsettled market in markout-only mode --
#: solely to satisfy `MarketCandles.__post_init__`'s `result in
#: SETTLEABLE_RESULTS` requirement (`app.scripts.mm_backtest`, unmodified
#: by this kit). NEVER read as a real settlement, and arithmetically
#: inert: `replay_snapshots(..., markout_only=True)` always strips the
#: settlement term `terminal_inventory * (settle - last_mid)` back out of
#: every row's `markout_pnl` (`_strip_terminal_settlement`) before it is
#: reported, so the choice of `"no"` over `"yes"` changes nothing in the
#: output -- it is pinned to one value only so a reader inspecting a
#: `MarketCandles` object does not mistake a missing result for "yes by
#: default" (module docstring, "MARKOUT-ONLY MODE").
_MARKOUT_PLACEHOLDER_RESULT = "no"

#: `terminal=` this module reports in markout-only mode (GUARDRAILS.md
#: §2.1). Distinct from `app.scripts.mm_backtest.TERMINAL` ("settled"),
#: which `report()` always writes into its own dict -- `to_markout_only_
#: report()` overwrites every occurrence of it after calling `report()`,
#: because markout-only mode's whole premise (module docstring, point 3)
#: is that terminal inventory's settlement contribution has been stripped
#: from `markout_pnl` for every row, settled or not.
MARKOUT_ONLY_TERMINAL = "excluded"

#: Sentinel `report()`'s STRING-typed cash-basis fields (`pnl_basis`)
#: carry in markout-only mode. The corresponding NUMERIC fields
#: (`total_pnl`, `roc`, the CI bounds, the power table's per-size
#: `pct5_total_pnl`/`p_profit`) are set to `None`, never `0.0` and never
#: this string -- `_fmt`/`_print_model` (`app.scripts.mm_backtest`,
#: reused unmodified) already render a bare `None` as a dash rather than
#: a number, so nulling the numeric slots keeps the printed table working
#: with no second print implementation.
CASH_PNL_UNAVAILABLE = "n/a — requires settlement"


# ---------------------------------------------------------------------------
# 1. Loading and diagnostics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SnapshotLoadResult:
    """What `load_market_candles` found, excluded, and why (the report header).

    Every count here is over the CANDIDATE set -- markets with at least one
    `book_snapshots` row for `venue`'s YES outcome in `[since, until]` --
    computed BEFORE any market is handed to `replay()`, so a reader can see
    exactly what evidence existed and what happened to each piece of it.

    Attributes:
        venue: Venue replayed.
        since: Query window start, aware UTC (inclusive).
        until: Query window end, aware UTC (inclusive).
        n_snapshot_rows: Total `book_snapshots` rows read.
        n_candidate_markets: Distinct `market_id`s among those rows.
        n_short_history: Candidate markets with fewer than `MIN_CANDLES`
            snapshots -- too little history to form even one
            (quote, fill, mark) triple (mirrors T2's `n_short_history`).
        n_unsettled: Candidate markets with enough history but no
            settleable result at analysis time -- not found in the venue's
            `resolved` listing, or found with `result` outside
            `{"yes","no"}`. EXCLUDED from `MarketCandles`/the verdict and
            counted here, never assigned a mark (PLAN.md D4,
            GUARDRAILS.md §4.2).
        n_conversion_errors: Candidate markets whose snapshot rows could not
            be turned into valid `Candle`/`OrderBook` objects (a corrupt
            stored level, an out-of-range price) -- counted rather than
            crashing the whole replay, the same "one bad market costs one
            market" discipline T16 applied to collection itself.
        n_unknown_volume_intervals: Consecutive-snapshot intervals, among
            REPLAYED markets, excluded from BOTH fill models because the
            `volume_lifetime` delta between them could not be trusted
            (either side `None`, or a negative delta -- see
            `_volume_delta`). Never defaulted to `0.0` or `1.0`
            (GUARDRAILS.md §3.3; the module docstring's "THE CORRECTION").
            Counted only over intervals `replay()` actually reads as a fill
            candidate (excludes the series' first and last snapshot, which
            are never addressed as a fill interval -- see
            `snapshots_to_candles`).
        n_incomplete_fill_book_intervals: Consecutive-snapshot intervals
            where the volume delta WAS known but the later snapshot's own
            book was not a valid two-sided quote (missing or crossed), so no
            `TradeRange` could be built from it either. A DIFFERENT gap from
            `n_unknown_volume_intervals` -- this one is about book
            completeness, not volume -- counted separately so the two are
            never conflated.
        n_replayed_markets: Markets actually handed to `replay()`.
        first_ts: Earliest snapshot `ts` among replayed markets, or `None`.
        last_ts: Latest snapshot `ts` among replayed markets, or `None`.
        median_observed_dwell_s: Median gap, in seconds, between consecutive
            `observed_at` values within a market's series, pooled across
            every replayed market, or `None` if no market had 2+ rows with
            `observed_at` set. See the module docstring's "`observed_at`,
            NOT `ts`" section for what a large gap between this and the
            `ts`-derived span means.
        markout_only: Whether this load ran in markout-only mode (module
            docstring, "MARKOUT-ONLY MODE"). `False` (settled mode) is
            the default and matches every prior release's behaviour.
        n_no_market_metadata: Candidate markets with no settleable result
            AND no listing entry at all (any status) to supply
            `event_id`/`close_time` from -- excluded even in markout-only
            mode, because `MarketCandles` cannot be built without them.
            Always `0` in settled mode (unreachable there: a market with
            no listing entry is already counted and skipped as
            `n_unsettled` before this check would run).
    """

    venue: VenueId
    since: datetime
    until: datetime
    n_snapshot_rows: int
    n_candidate_markets: int
    n_short_history: int
    n_unsettled: int
    n_conversion_errors: int
    n_unknown_volume_intervals: int
    n_incomplete_fill_book_intervals: int
    n_replayed_markets: int
    first_ts: datetime | None
    last_ts: datetime | None
    median_observed_dwell_s: float | None
    markout_only: bool = False
    n_no_market_metadata: int = 0

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe representation, embedded as `data_window` in the report."""
        return {
            "venue": self.venue,
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "n_snapshot_rows": self.n_snapshot_rows,
            "n_candidate_markets": self.n_candidate_markets,
            "n_short_history": self.n_short_history,
            "n_unsettled": self.n_unsettled,
            "n_conversion_errors": self.n_conversion_errors,
            "n_unknown_volume_intervals": self.n_unknown_volume_intervals,
            "n_incomplete_fill_book_intervals": self.n_incomplete_fill_book_intervals,
            "n_replayed_markets": self.n_replayed_markets,
            "first_ts": self.first_ts.isoformat() if self.first_ts else None,
            "last_ts": self.last_ts.isoformat() if self.last_ts else None,
            "median_observed_dwell_s": self.median_observed_dwell_s,
            "markout_only": self.markout_only,
            "n_no_market_metadata": self.n_no_market_metadata,
        }


@dataclass(frozen=True)
class LoadedMarket:
    """One market ready for `replay()`, plus the tick size it must be quoted at.

    T2's `MarketCandles` carries no `tick_size` field -- `replay()` takes
    ONE scalar tick size for its whole call. This wrapper carries the tick
    size actually observed on the market's own snapshots (its FIRST row's
    `tick_size`, which is expected to be constant for a market's life), so
    `replay_snapshots()` can group markets by it instead of assuming one
    global value (module docstring, "REUSE, NOT REIMPLEMENTATION").

    Attributes:
        candles: The market, ready for `replay()`.
        tick_size: Minimum price increment, a probability in `(0.0, 1.0]`.
    """

    candles: MarketCandles
    tick_size: float


def _volume_delta(previous: float | None, current: float | None) -> float | None:
    """The lifetime-volume delta between two consecutive same-key snapshots.

    Args:
        previous: The earlier snapshot's `volume_lifetime`.
        current: The later snapshot's `volume_lifetime`.

    Returns:
        float | None: `current - previous` if both are present and the
            result is `>= 0.0`. `None` -- UNKNOWN, never `0.0` and never a
            negative -- when either side is `None` (never populated, or
            T15's write-time monotonicity guard already caught a
            restatement there) OR when the delta computed here is itself
            negative. That second case is deliberately paranoid: T15's own
            guard (`_monotonic_lifetime_volume`) only compares a poll
            against the immediately preceding KNOWN reading for that
            `(venue, market_id, outcome)`, which need not be the row
            immediately before this one in the sequence THIS function is
            handed (an intervening poll's restatement could already have
            been resolved against a reading this function never sees).
            Re-checking here never turns a real decrease into a fabricated
            fill; it can only additionally exclude a delta the write-time
            guard let through. `passive_fill.py:97` raises `ValueError` on
            a negative volume and `:142` treats `0.0` as "definitely
            nothing traded" -- neither is a value this function may return
            for a reading it cannot vouch for.
    """
    if previous is None or current is None:
        return None
    delta = current - previous
    return delta if delta >= 0.0 else None


def _order_book(row: BookSnapshot) -> OrderBook:
    """Rebuild the observed `OrderBook` from one `book_snapshots` row.

    Reuses `OrderBook`'s own sorting and range validation (best-price-first,
    `BookLevel`'s `_check_price`/`_check_size`) instead of assuming the
    stored `bids`/`asks` JSON is already ordered -- the same reconstruction
    `app.services.backtesting.data_replay.DataReplayer._get_recorded_book`
    performs for this table.

    Raises:
        ValueError: If a stored level's `price`/`size` is out of range --
            propagated to the caller, which counts it as a conversion error
            rather than crashing the whole replay.
    """
    return OrderBook(
        venue=row.venue,
        market_id=row.market_id,
        outcome=row.outcome,
        bids=tuple(
            BookLevel(price=level["price"], size=level["size"]) for level in row.bids
        ),
        asks=tuple(
            BookLevel(price=level["price"], size=level["size"]) for level in row.asks
        ),
        ts=_ensure_utc(row.ts),
    )


@dataclass(frozen=True)
class _CandleBuild:
    """`snapshots_to_candles`'s result: the candles, and what got excluded."""

    candles: tuple[Candle, ...]
    n_unknown_volume_intervals: int
    n_incomplete_fill_book_intervals: int


def snapshots_to_candles(rows: Sequence[BookSnapshot]) -> _CandleBuild:
    """Adapt one market's sorted `BookSnapshot` rows into T2's `Candle` shape.

    `rows` must already be sorted ascending by `ts` (`_fetch_snapshot_rows`
    guarantees this) -- the same "ascending by `end_ts`" contract T2's
    `MarketCandles.candles` documents. Candle `j`'s `bid_close`/`ask_close`
    are snapshot `j`'s own best bid/ask; candle `j`'s `px_low`/`px_high`/
    `volume` describe the interval ENDING at snapshot `j` (see the module
    docstring's "THE MAPPING"). The very FIRST candle's `px_low`/`px_high`/
    `volume` are placeholders (`None`/`None`/`0.0`) that satisfy `Candle`'s
    own validation but are never read: `replay()` only ever consults candle
    `j`'s trade fields when `j` is addressed as `candles[i + 1]` for some
    `i` in `range(len(candles) - 2)`, i.e. `j` in `[1, len(candles) - 2]` --
    the first candle (`j == 0`) and the last (`j == len(candles) - 1`) are
    never addressed that way, which is also why the exclusion counts below
    are computed only over that same relevant range: counting either end
    would overstate how much evidence `replay()` actually discarded.

    Args:
        rows: One market's snapshot rows, `outcome == "YES"`, ascending by
            `ts`.

    Returns:
        _CandleBuild: The `Candle` tuple (same length as `rows`) and the two
            exclusion counts.

    Raises:
        ValueError: If a row's stored book cannot be rebuilt
            (`_order_book`) or a resulting price is out of `[0.0, 1.0]`
            (`Candle.__post_init__`) -- the caller (`load_market_candles`)
            counts this as a conversion error for the whole market rather
            than catching it per-candle.
    """
    candles: list[Candle] = []
    # Index k here corresponds to candle j = k + 1 (there is one flag per
    # candle from the second one onward); see the docstring's "relevant
    # range" note for why only a slice of these two lists is ever counted.
    unknown_volume_flags: list[bool] = []
    incomplete_book_flags: list[bool] = []
    previous_row: BookSnapshot | None = None

    for row in rows:
        book = _order_book(row)
        best_bid = book.best_bid()
        best_ask = book.best_ask()
        bid_close = best_bid.price if best_bid is not None else None
        ask_close = best_ask.price if best_ask is not None else None

        if previous_row is None:
            px_low = px_high = None
            volume = 0.0
        else:
            delta = _volume_delta(previous_row.volume_lifetime, row.volume_lifetime)
            if delta is None:
                unknown_volume_flags.append(True)
                incomplete_book_flags.append(False)
                px_low = px_high = None
                volume = 0.0
            else:
                two_sided = (
                    bid_close is not None
                    and ask_close is not None
                    and bid_close <= ask_close
                )
                unknown_volume_flags.append(False)
                incomplete_book_flags.append(not two_sided)
                px_low, px_high = (bid_close, ask_close) if two_sided else (None, None)
                volume = delta

        candles.append(
            Candle(
                end_ts=int(_ensure_utc(row.ts).timestamp()),
                bid_close=bid_close,
                ask_close=ask_close,
                px_low=px_low,
                px_high=px_high,
                px_close=None,
                volume=volume,
                open_interest=None,
            )
        )
        previous_row = row

    n = len(candles)
    # flags[k] describes candle j = k + 1; the relevant range for j is
    # [1, n - 2], i.e. k in [0, n - 3] -- a slice stopping at n - 2.
    relevant = slice(0, max(0, n - 2))
    n_unknown_volume = sum(1 for flag in unknown_volume_flags[relevant] if flag)
    n_incomplete_book = sum(1 for flag in incomplete_book_flags[relevant] if flag)

    return _CandleBuild(
        candles=tuple(candles),
        n_unknown_volume_intervals=n_unknown_volume,
        n_incomplete_fill_book_intervals=n_incomplete_book,
    )


def fee_schedule_for_market(
    rows: Sequence[BookSnapshot], fallback: FeeSchedule
) -> tuple[FeeSchedule, bool]:
    """The fee schedule to replay one market under, and whether it is a fallback.

    `PassiveFillEngine` is built ONCE per market inside `replay()`, against
    ONE `FeeSchedule` -- there is no per-interval fee parameter, so a market
    whose fee changed mid-series is necessarily replayed under a single
    representative schedule (module docstring, "FEES"). This walks `rows`
    from the MOST RECENT backward and returns the first one carrying a
    COMPLETE reading -- `taker_fee_rate`, `maker_fee_rate`, `fee_source` AND
    `maker_rebate_rate` all non-`None` -- rather than accepting a partial
    one: these four are written together by `_upsert_book_snapshot` from the
    same `market.fee` object each poll, so a row missing even one of them is
    not a genuine "no rebate"/"no maker rate" observation, only a gap this
    function must not paper over with a default.

    Args:
        rows: One market's snapshot rows, any order.
        fallback: Schedule to use if no row has a complete reading (e.g.
            every row predates migration `008`).

    Returns:
        tuple[FeeSchedule, bool]: The schedule, and whether it is `fallback`
            (`True`) rather than one read from a snapshot (`False`).
    """
    for row in reversed(rows):
        if (
            row.taker_fee_rate is not None
            and row.maker_fee_rate is not None
            and row.fee_source is not None
            and row.maker_rebate_rate is not None
        ):
            return (
                FeeSchedule(
                    taker_rate=row.taker_fee_rate,
                    maker_rate=row.maker_fee_rate,
                    source=row.fee_source,
                    maker_rebate_rate=row.maker_rebate_rate,
                ),
                False,
            )
    return fallback, True


async def _fetch_snapshot_rows(
    session: AsyncSession, venue: VenueId, *, since: datetime, until: datetime
) -> dict[str, list[BookSnapshot]]:
    """Every `book_snapshots` row for `venue`'s YES outcome in `[since, until]`.

    Grouped by `market_id`, each list ascending by `ts` -- the order
    `snapshots_to_candles`/T2's `replay()` require. Only the YES outcome is
    read (module docstring, `_OUTCOME_YES`).

    Args:
        session: Open session to query. Tests pass the `test_session`
            fixture; `_main` opens one from `async_session_factory`.
        venue: `"kalshi"` or `"polymarket"`.
        since: Window start, aware UTC (inclusive).
        until: Window end, aware UTC (inclusive).

    Returns:
        dict[str, list[BookSnapshot]]: `market_id -> rows`, ascending `ts`.
    """
    ensure_aware(since)
    ensure_aware(until)
    stmt = (
        select(BookSnapshot)
        .where(
            BookSnapshot.venue == venue,
            BookSnapshot.outcome == _OUTCOME_YES,
            BookSnapshot.ts >= since,
            BookSnapshot.ts <= until,
        )
        .order_by(BookSnapshot.market_id, BookSnapshot.ts)
    )
    result = await session.execute(stmt)
    grouped: dict[str, list[BookSnapshot]] = defaultdict(list)
    for row in result.scalars():
        grouped[row.market_id].append(row)
    return grouped


async def resolved_markets(adapter: Any) -> dict[str, VenueMarket]:
    """Every resolved `VenueMarket` on `adapter`'s venue, indexed by `market_id`.

    Fetched at ANALYSIS time (PLAN.md D4) -- never cached across runs, and
    never the market's status at collection time. A `market_id` absent from
    the returned mapping has no settlement price yet and must be reported
    `unsettled`, not assumed resolved-but-missing.

    Args:
        adapter: Anything exposing `async list_markets(status=...)` -- the
            live `KalshiAdapter`/`PolymarketAdapter`, or a stub in tests.

    Returns:
        dict[str, VenueMarket]: `market_id -> VenueMarket`, `status ==
            "resolved"` only.
    """
    markets = await adapter.list_markets(status="resolved")
    return {m.market_id: m for m in markets}


async def all_markets(adapter: Any) -> dict[str, VenueMarket]:
    """Every `VenueMarket` on `adapter`'s venue, ANY status, by `market_id`.

    Markout-only mode's counterpart to `resolved_markets` (module
    docstring, "MARKOUT-ONLY MODE" point 1): it replays markets with no
    settlement yet, and those still need `event_id`/`close_time` to build
    a `MarketCandles` -- data that lives on the same listing payload
    regardless of status. `status=None` is the adapter's own "every
    status" value (`app.venues.base.VenueAdapter.list_markets`), not a
    new endpoint: still the one read-only public GET this module has
    ever made (GUARDRAILS.md §1.1/§1.3/§3.1).

    Args:
        adapter: Anything exposing `async list_markets(status=...)` -- the
            live `KalshiAdapter`/`PolymarketAdapter`, or a stub in tests.

    Returns:
        dict[str, VenueMarket]: `market_id -> VenueMarket`, any status.
    """
    markets = await adapter.list_markets(status=None)
    return {m.market_id: m for m in markets}


async def load_market_candles(
    session: AsyncSession,
    venue: VenueId,
    *,
    since: datetime,
    until: datetime,
    resolved: Mapping[str, VenueMarket],
    fallback_schedule: FeeSchedule,
    markout_only: bool = False,
) -> tuple[list[LoadedMarket], SnapshotLoadResult]:
    """Load, filter, and convert `book_snapshots` into `replay()`-ready markets.

    Order of exclusion, mirroring T2's `settled_universe`/`collect` split:
    settleability is checked FIRST (a market with no settlement price is
    `unsettled`, regardless of how much history it has), THEN conversion
    (`snapshots_to_candles`), THEN the `MIN_CANDLES` history floor.

    Args:
        session: Open session to query.
        venue: `"kalshi"` or `"polymarket"`.
        since: Query window start, aware UTC.
        until: Query window end, aware UTC.
        resolved: `market_id -> VenueMarket`, fetched at analysis time --
            every RESOLVED market on this venue (`resolved_markets`) in
            settled mode, or every market of ANY status (`all_markets`)
            when the caller is markout-only mode.
        fallback_schedule: Fee schedule for a market with no complete
            per-snapshot fee reading (`fee_schedule_for_market`).
        markout_only: When `True` (module docstring, "MARKOUT-ONLY
            MODE"), a candidate market with no settleable result is
            INCLUDED rather than excluded, using
            `_MARKOUT_PLACEHOLDER_RESULT` in place of a real `result` --
            arithmetically inert, because `replay_snapshots(...,
            markout_only=True)` always strips the settlement term back
            out of `markout_pnl` regardless of which result was used.
            Still requires the market to appear SOMEWHERE in `resolved`
            (any status) for its `event_id`/`close_time`; a candidate
            with no listing entry at all is counted
            (`n_no_market_metadata`) and excluded either way. Default
            `False` -- settled mode's existing behaviour, unchanged.

    Returns:
        tuple[list[LoadedMarket], SnapshotLoadResult]: Markets ready for
            `replay_snapshots()`, and the counts behind every exclusion.
    """
    ensure_aware(since)
    ensure_aware(until)
    grouped = await _fetch_snapshot_rows(session, venue, since=since, until=until)

    n_short_history = 0
    n_unsettled = 0
    n_no_market_metadata = 0
    n_conversion_errors = 0
    n_unknown_volume = 0
    n_incomplete_book = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    dwell_seconds: list[float] = []
    loaded: list[LoadedMarket] = []

    for market_id in sorted(grouped):
        rows = grouped[market_id]
        venue_market = resolved.get(market_id)
        is_settled = (
            venue_market is not None
            and (venue_market.result or "") in SETTLEABLE_RESULTS
        )
        if not is_settled:
            n_unsettled += 1
            if not markout_only:
                # Settled mode: unsettled is exclusion, full stop (PLAN.md
                # D4, GUARDRAILS.md §4.2) -- unchanged from every prior
                # release.
                continue
            if venue_market is None:
                # Markout-only mode still needs event_id/close_time from
                # SOMEWHERE to build a MarketCandles; a market absent
                # from every status this venue reports has none to give.
                n_no_market_metadata += 1
                continue

        try:
            built = snapshots_to_candles(rows)
        except ValueError:
            n_conversion_errors += 1
            continue

        if len(built.candles) < MIN_CANDLES:
            n_short_history += 1
            continue

        fee, _is_fallback = fee_schedule_for_market(rows, fallback_schedule)
        result_for_candles = (
            venue_market.result if is_settled else _MARKOUT_PLACEHOLDER_RESULT
        )
        loaded.append(
            LoadedMarket(
                candles=MarketCandles(
                    venue=venue,
                    market_id=market_id,
                    event=venue_market.event_id or market_id,
                    series=series_for(venue_market) if venue == "kalshi" else "",
                    close_ts=int(ensure_aware(venue_market.close_time).timestamp()),
                    result=result_for_candles,
                    candles=built.candles,
                    fee=fee,
                    outcome=_OUTCOME_YES,
                ),
                tick_size=rows[0].tick_size,
            )
        )
        n_unknown_volume += built.n_unknown_volume_intervals
        n_incomplete_book += built.n_incomplete_fill_book_intervals

        row_ts = [_ensure_utc(r.ts) for r in rows]
        first_ts = row_ts[0] if first_ts is None else min(first_ts, row_ts[0])
        last_ts = row_ts[-1] if last_ts is None else max(last_ts, row_ts[-1])
        observed = sorted(
            _ensure_utc(r.observed_at) for r in rows if r.observed_at is not None
        )
        for earlier, later in zip(observed, observed[1:], strict=False):
            dwell_seconds.append((later - earlier).total_seconds())

    diagnostics = SnapshotLoadResult(
        venue=venue,
        since=since,
        until=until,
        n_snapshot_rows=sum(len(v) for v in grouped.values()),
        n_candidate_markets=len(grouped),
        n_short_history=n_short_history,
        n_unsettled=n_unsettled,
        n_conversion_errors=n_conversion_errors,
        n_unknown_volume_intervals=n_unknown_volume,
        n_incomplete_fill_book_intervals=n_incomplete_book,
        n_replayed_markets=len(loaded),
        first_ts=first_ts,
        last_ts=last_ts,
        median_observed_dwell_s=(
            statistics.median(dwell_seconds) if dwell_seconds else None
        ),
        markout_only=markout_only,
        n_no_market_metadata=n_no_market_metadata,
    )
    return loaded, diagnostics


# ---------------------------------------------------------------------------
# 2. Replay -- grouped by tick size, merged, then T2's report()
# ---------------------------------------------------------------------------


def _merge_replay_results(results: Sequence[ReplayResult]) -> ReplayResult:
    """Combine same-`fill_model` `ReplayResult`s from separate `replay()` calls.

    Pure bookkeeping -- concatenates `rows` and sums the interval counters.
    No replay arithmetic happens here; every `MarketRow` was already
    produced by T2's own `replay()`.

    Raises:
        ValueError: If `results` mix more than one `fill_model`, or is empty.
    """
    if not results:
        raise ValueError("cannot merge zero ReplayResult objects")
    fill_models = {r.fill_model for r in results}
    if len(fill_models) != 1:
        raise ValueError(
            f"cannot merge ReplayResult objects from different fill models: {fill_models!r}"
        )
    rows: list[MarketRow] = []
    n_intervals = 0
    n_unmarkable = 0
    for r in results:
        rows.extend(r.rows)
        n_intervals += r.n_intervals
        n_unmarkable += r.n_unmarkable
    return ReplayResult(
        fill_model=next(iter(fill_models)),
        rows=tuple(rows),
        n_intervals=n_intervals,
        n_unmarkable=n_unmarkable,
    )


def _strip_terminal_settlement(candles: MarketCandles, row: MarketRow) -> MarketRow:
    """`row.markout_pnl` with the terminal-inventory settlement term removed.

    Markout-only mode's core correction (module docstring, "MARKOUT-ONLY
    MODE" point 3). `replay()` (`app.scripts.mm_backtest`, unmodified)
    folds `terminal_inventory * (candles.settle - row.last_mid)` into
    `markout_pnl` whenever the market ends with open inventory that has
    filled at least once (`replay()`'s own "Terminal inventory settles at
    the venue's real result" comment) -- correct when `candles.settle` is
    a REAL result, undefined when it is `_MARKOUT_PLACEHOLDER_RESULT`
    standing in for a market with no result yet. Rather than branch on
    which case applies, this function reverses the IDENTICAL arithmetic
    `replay()` used to add the term, for every row alike: the two read
    the same `candles.settle`/`row.terminal_inventory`/`row.last_mid`, so
    they are exact inverses by construction. A market with a real result
    loses exactly what its terminal inventory contributed; a market with
    a placeholder loses exactly what the placeholder invented. Either
    way, what remains is the pure per-fill sum -- `markout_pnl` as if
    `held_into_settlement` never happened.

    Args:
        candles: The market `row` was replayed from -- the SAME object
            whose `.settle` `replay()` itself read for this row.
        row: One `MarketRow` `replay()` produced for `candles`.

    Returns:
        MarketRow: `row` with `markout_pnl` reduced by the settlement
            term (unchanged if `terminal_inventory == 0.0` or the market
            never filled, i.e. `last_mid is None` -- the term `replay()`
            would have added was already `0.0` in both cases). Every
            other field, including `pnl` (still cash-settled at the
            possibly-placeholder result; callers must not report it in
            markout-only mode -- see `to_markout_only_report`) and
            `terminal_inventory` itself (kept for the `held_into_
            settlement` diagnostic), is untouched.
    """
    term = (
        row.terminal_inventory * (candles.settle - row.last_mid)
        if row.terminal_inventory != 0.0 and row.last_mid is not None
        else 0.0
    )
    return replace(row, markout_pnl=row.markout_pnl - term)


def replay_snapshots(
    markets: Sequence[LoadedMarket],
    *,
    policy: MarketMaker,
    fill_model: FillModel,
    fee_model: FeeModel,
    schedule: FeeSchedule,
    markout_only: bool = False,
) -> ReplayResult:
    """Replay every market through T2's `replay()`, grouped by tick size.

    Groups `markets` by their own observed `tick_size` (module docstring,
    "REUSE, NOT REIMPLEMENTATION") and calls `app.scripts.mm_backtest.replay`
    once per distinct value, merging the results. Buckets are processed in
    ascending tick-size order, and within a bucket in `markets`' own input
    order -- deterministic given a fixed input, so a `report()` computed
    from this with a fixed `--seed` reproduces exactly.

    Args:
        markets: Markets to replay (`load_market_candles`'s first return
            value).
        policy: The quoting policy.
        fill_model: `"optimistic"` or `"pessimistic"`.
        fee_model: Venue fee model (`KalshiFeeModel`/`PolymarketFeeModel`).
        schedule: Fallback schedule for a market carrying none of its own
            (a market's own `MarketCandles.fee` always wins -- T2's
            `replay()` docstring).
        markout_only: When `True` (module docstring, "MARKOUT-ONLY
            MODE"), every row `replay()` returns has its terminal-
            inventory settlement term stripped from `markout_pnl`
            (`_strip_terminal_settlement`) before merging, and the
            returned `ReplayResult.terminal` is `MARKOUT_ONLY_TERMINAL`
            rather than T2's `TERMINAL`. `replay()` itself is called
            exactly as settled mode calls it either way -- this flag
            changes nothing upstream of `replay()`'s own return.

    Returns:
        ReplayResult: THE SAME dataclass T2's `replay()` returns, merged
            across tick-size buckets.
    """
    buckets: dict[float, list[MarketCandles]] = defaultdict(list)
    for loaded in markets:
        buckets[loaded.tick_size].append(loaded.candles)

    if not buckets:
        empty = ReplayResult(fill_model=fill_model, rows=(), n_intervals=0, n_unmarkable=0)
        return replace(empty, terminal=MARKOUT_ONLY_TERMINAL) if markout_only else empty

    results = []
    for tick in sorted(buckets):
        bucket_candles = buckets[tick]
        bucket_result = replay(
            bucket_candles,
            policy=policy,
            fill_model=fill_model,
            tick_size=tick,
            fee_model=fee_model,
            schedule=schedule,
        )
        if markout_only:
            bucket_result = replace(
                bucket_result,
                rows=tuple(
                    _strip_terminal_settlement(candles, row)
                    for candles, row in zip(
                        bucket_candles, bucket_result.rows, strict=True
                    )
                ),
            )
        results.append(bucket_result)

    merged = _merge_replay_results(results)
    return replace(merged, terminal=MARKOUT_ONLY_TERMINAL) if markout_only else merged


# ---------------------------------------------------------------------------
# 3. Venue wiring
# ---------------------------------------------------------------------------


def fee_model_and_fallback(venue: VenueId) -> tuple[FeeModel, FeeSchedule]:
    """The `FeeModel` and default `FeeSchedule` for `venue` (GUARDRAILS.md §1.5).

    Mirrors T2's own Kalshi defaults (`KalshiFeeModel`/`default_kalshi_
    schedule`) and picks the analogous pair for Polymarket:
    `PolymarketFeeModel` (makers pay 0 regardless of `schedule.maker_rate`)
    and `category_fee_schedule(None)` -- the same "unknown category" 5%
    taker default `app.venues.fees.category_rate` falls back to, used ONLY
    when a market has no complete per-snapshot fee reading at all
    (`fee_schedule_for_market`).

    Raises:
        ValueError: If `venue` is not `"kalshi"` or `"polymarket"`.
    """
    if venue == "kalshi":
        return KalshiFeeModel(), default_kalshi_schedule()
    if venue == "polymarket":
        return PolymarketFeeModel(), category_fee_schedule(None)
    raise ValueError(f"venue must be 'kalshi' or 'polymarket', got {venue!r}")


def _adapter_for_venue(venue: VenueId) -> Any:
    """Build the live read-path adapter for `venue` (never the live-trading one).

    Imported locally, matching `app/venues/registry.py`/`app/venues/paper.py`'s
    own lazy-import pattern for these two adapters.
    """
    if venue == "kalshi":
        from app.venues.kalshi.adapter import KalshiAdapter

        return KalshiAdapter()
    if venue == "polymarket":
        from app.venues.polymarket.adapter import PolymarketAdapter

        return PolymarketAdapter()
    raise ValueError(f"venue must be 'kalshi' or 'polymarket', got {venue!r}")


# ---------------------------------------------------------------------------
# 4. CLI
# ---------------------------------------------------------------------------


def render_header(diagnostics: SnapshotLoadResult) -> str:
    """Render the data-window header -- printed whether or not there is
    anything to replay (brief: "prints the data window (empty -> clear exit
    1)")."""
    d = diagnostics
    lines = [
        "=" * 78,
        "mm_replay_snapshots -- passive quoting replayed over book_snapshots",
        "=" * 78,
    ]
    if d.markout_only:
        lines.append(
            "mode: MARKOUT-ONLY -- unsettled markets included; cash P&L is"
            f" {CASH_PNL_UNAVAILABLE}; terminal={MARKOUT_ONLY_TERMINAL}"
        )
    lines.append(
        f"data window: venue={d.venue} since={d.since.isoformat()} until={d.until.isoformat()}"
    )
    lines.append(
        f"             snapshot rows {d.n_snapshot_rows}"
        f"  candidate markets {d.n_candidate_markets}"
        f"  replayed {d.n_replayed_markets}"
    )
    if d.markout_only:
        lines.append(
            f"             excluded: short history {d.n_short_history}"
            f"  conversion errors {d.n_conversion_errors}"
            f"  no market metadata {d.n_no_market_metadata}"
        )
        lines.append(
            f"             n_unsettled {d.n_unsettled} (INCLUDED, not excluded,"
            " in markout-only mode -- module docstring)"
        )
    else:
        lines.append(
            f"             excluded: short history {d.n_short_history}"
            f"  unsettled {d.n_unsettled}"
            f"  conversion errors {d.n_conversion_errors}"
        )
    lines.extend(
        [
            f"             n_unknown_volume_intervals {d.n_unknown_volume_intervals}"
            " (volume_lifetime delta unknown -- excluded from BOTH fill models,"
            " never defaulted; GUARDRAILS.md 3.3)",
            f"             n_incomplete_fill_book_intervals {d.n_incomplete_fill_book_intervals}"
            " (fill snapshot's own book was one-sided or crossed)",
        ]
    )
    if d.first_ts is not None and d.last_ts is not None:
        lines.append(
            f"             snapshots span {d.first_ts.isoformat()} -> {d.last_ts.isoformat()}"
        )
    if d.median_observed_dwell_s is not None:
        lines.append(
            "             median seconds between snapshots (by observed_at,"
            f" not ts): {d.median_observed_dwell_s:.1f}"
        )
        lines.append(
            "             NOTE: ts is book-move time, not poll time -- a"
            " span much wider than this dwell means a few long-quiet books,"
            " not many short intervals (see module docstring)."
        )
    return "\n".join(lines)


def _print_policy(policy: MarketMaker, fee_model: FeeModel, fallback: FeeSchedule) -> None:
    print(
        f"policy:      min_spread={policy.min_spread}"
        f" edge_fraction={policy.edge_fraction}"
        f" max_inventory={policy.max_inventory}"
        f" skew_strength={policy.skew_strength}"
        f" quote_size={policy.quote_size}"
    )
    print(
        f"fees:        {type(fee_model).__name__}"
        f" fallback_taker_rate={fallback.taker_rate}"
        f" fallback_maker_rate={fallback.maker_rate}"
        f" fallback_source={fallback.source}"
    )


def _markout_only_block(block: dict[str, Any]) -> dict[str, Any]:
    """One `report()` `_block()` dict, converted to markout-only mode.

    Module docstring, "MARKOUT-ONLY MODE" point 2/3. Every MARKOUT figure
    (`total_markout_pnl`, `mean_markout_pnl_per_trading_market`,
    `sd_markout_pnl_per_trading_market`) passes through UNCHANGED --
    `replay_snapshots(..., markout_only=True)` already stripped the
    settlement term from each row's `markout_pnl` before `report()` ever
    aggregated them (`_strip_terminal_settlement`), so nothing here
    recomputes a statistic `report()` itself produced (PLAN.md D11: one
    report implementation). This function only relabels `terminal`,
    replaces the CASH-basis figures with `None` (never `0.0`) since
    `pnl`'s rows may be settled at `_MARKOUT_PLACEHOLDER_RESULT`, and adds
    the per-fill markout figure.

    Args:
        block: One `_block()`-shaped dict from `report()`'s `overall`/
            `train`/`test`.

    Returns:
        dict[str, Any]: A copy of `block`. `total_pnl`/
            `mean_pnl_per_trading_market`/`sd_pnl_per_trading_market`/
            `roc`/`ci95_clustered_by_event`'s two bounds/the power
            table's `pct5_total_pnl` and `p_profit` at every portfolio
            size are `None`; `pnl_basis` is `CASH_PNL_UNAVAILABLE` (a
            string, so a caller cannot mistake it for a computed number);
            `mean_markout_pnl_per_fill` is new (`total_markout_pnl /
            n_fills`, `None` when `n_fills == 0` -- never a
            division-by-zero).
    """
    power = {
        size: {**cell, "pct5_total_pnl": None, "p_profit": None}
        for size, cell in block["power"].items()
    }
    n_fills = block["n_fills"]
    return {
        **block,
        "terminal": MARKOUT_ONLY_TERMINAL,
        "pnl_basis": CASH_PNL_UNAVAILABLE,
        "total_pnl": None,
        "mean_pnl_per_trading_market": None,
        "sd_pnl_per_trading_market": None,
        "ci95_clustered_by_event": [None, None],
        "roc": None,
        "power": power,
        "mean_markout_pnl_per_fill": (
            block["total_markout_pnl"] / n_fills if n_fills > 0 else None
        ),
    }


def to_markout_only_report(payload: dict[str, Any]) -> dict[str, Any]:
    """`report()`'s own return value, converted to markout-only mode's shape.

    Called ONLY after `report()` itself has produced `payload` from a
    `ReplayResult` whose rows already went through `_strip_terminal_
    settlement` (`replay_snapshots(..., markout_only=True)`) -- this
    function does not touch `markout_pnl` again; it relabels `terminal`,
    nulls the cash-basis figures (module docstring, "MARKOUT-ONLY MODE"),
    and drops the verdict entirely. The verdict IS the cash-basis go/
    no-go (`report()`'s own docstring: "computed on the cash figure") and
    has no markout-only analogue -- `report()` itself already establishes
    the convention of a present-but-`None` verdict (`split="event"`
    returns `verdict: None`), so returning `None` here is not a new
    convention, only a new reason for the same one.

    Args:
        payload: `report()`'s return value, straight from the import --
            same dict shape `app.scripts.mm_backtest.report`'s own
            docstring documents.

    Returns:
        dict[str, Any]: `payload` with `terminal`/`overall`/`train`/
            `test`/`split_exclusions`/`verdict` replaced; every other key
            (`fill_model`, `split`, `cutoff_ts`, `seed`, `n_intervals`,
            `n_unmarkable_intervals`) passes through unchanged.
    """
    return {
        **payload,
        "terminal": MARKOUT_ONLY_TERMINAL,
        "overall": _markout_only_block(payload["overall"]),
        "train": _markout_only_block(payload["train"]),
        "test": _markout_only_block(payload["test"]),
        "split_exclusions": {
            **payload["split_exclusions"],
            "terminal": MARKOUT_ONLY_TERMINAL,
        },
        "verdict": None,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay MarketMaker over collected book_snapshots (T11)."
    )
    parser.add_argument(
        "--venue", required=True, choices=_VENUES, help="venue to replay"
    )
    parser.add_argument(
        "--from", dest="from_", type=str, default=None,
        help="ISO datetime; snapshots before this are excluded (default: epoch)",
    )
    parser.add_argument(
        "--to", dest="to_", type=str, default=None,
        help="ISO datetime; snapshots after this are excluded (default: now)",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--temporal-cutoff", type=str, default=None,
        help="YYYY-MM-DD; train is closes BEFORE it, test at or after. "
             "Defaults to the median close.",
    )
    parser.add_argument("--out", type=str, default=None, help="write the report JSON here")
    parser.add_argument(
        "--markout-only", action="store_true",
        help="include unsettled markets and score markout_pnl only (module "
             "docstring 'MARKOUT-ONLY MODE'); cash P&L is reported as "
             f"{CASH_PNL_UNAVAILABLE!r}, never a number, and terminal="
             f"{MARKOUT_ONLY_TERMINAL!r} rather than 'settled'",
    )
    parser.add_argument("--min-spread", type=float, default=DEFAULT_MIN_SPREAD)
    parser.add_argument("--edge-fraction", type=float, default=DEFAULT_EDGE_FRACTION)
    parser.add_argument("--max-inventory", type=float, default=DEFAULT_MAX_INVENTORY)
    parser.add_argument("--skew-strength", type=float, default=DEFAULT_SKEW_STRENGTH)
    parser.add_argument("--quote-size", type=float, default=DEFAULT_QUOTE_SIZE)
    return parser


def _parse_bound(raw: str | None, *, default: datetime) -> datetime:
    """Parse `--from`/`--to`; `None` uses `default`. Naive input is UTC."""
    if raw is None:
        return default
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def _main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # See T2's `_main` for why: unformatted adapter warnings otherwise reach
    # `logging.lastResort` interleaved into stdout, which is the report.
    configure_logging(level="WARNING")

    venue: VenueId = args.venue
    since = _parse_bound(args.from_, default=datetime(1970, 1, 1, tzinfo=UTC))
    until = _parse_bound(args.to_, default=utcnow())
    if since > until:
        print(f"--from {since.isoformat()} is after --to {until.isoformat()}", file=sys.stderr)
        return 1

    fee_model, fallback_schedule = fee_model_and_fallback(venue)
    adapter = _adapter_for_venue(venue)

    # Markout-only mode still needs event_id/close_time for an unsettled
    # market, so it asks the adapter for every status instead of only
    # "resolved" (module docstring, "MARKOUT-ONLY MODE" point 1).
    market_lookup = (
        await all_markets(adapter)
        if args.markout_only
        else await resolved_markets(adapter)
    )
    async with async_session_factory() as session:
        loaded, diagnostics = await load_market_candles(
            session,
            venue,
            since=since,
            until=until,
            resolved=market_lookup,
            fallback_schedule=fallback_schedule,
            markout_only=args.markout_only,
        )

    print(render_header(diagnostics))

    if not loaded:
        # The correct behaviour before/while forward collection is still
        # thin (module docstring) -- exit 1 with a clear count, not a crash.
        print(
            "\nno markets with enough replayable history in this window;"
            " nothing to replay.",
            file=sys.stderr,
        )
        return 1

    policy = MarketMaker(
        min_spread=args.min_spread,
        edge_fraction=args.edge_fraction,
        max_inventory=args.max_inventory,
        quote_size=args.quote_size,
        skew_strength=args.skew_strength,
    )
    cutoff, cutoff_source = _cutoff_ts([m.candles for m in loaded], args.temporal_cutoff)

    payload: dict[str, Any] = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "data_window": {
            **diagnostics.to_dict(),
            "cutoff_ts": cutoff,
            "cutoff_source": cutoff_source,
            "cutoff": datetime.fromtimestamp(cutoff, tz=UTC).isoformat(),
        },
        "policy": {
            "min_spread": policy.min_spread,
            "edge_fraction": policy.edge_fraction,
            "max_inventory": policy.max_inventory,
            "skew_strength": policy.skew_strength,
            "quote_size": policy.quote_size,
            "fee_model": type(fee_model).__name__,
            "fallback_taker_rate": fallback_schedule.taker_rate,
            "fallback_maker_rate": fallback_schedule.maker_rate,
            "fallback_source": fallback_schedule.source,
            "seed": args.seed,
        },
    }
    # Pessimistic FIRST (GUARDRAILS.md §2.2): the verdict is computed on it.
    for fill_model in ("pessimistic", "optimistic"):
        result = replay_snapshots(
            loaded,
            policy=policy,
            fill_model=fill_model,
            fee_model=fee_model,
            schedule=fallback_schedule,
            markout_only=args.markout_only,
        )
        raw_report = report(result, split="temporal", cutoff_ts=cutoff, seed=args.seed)
        payload[fill_model] = (
            to_markout_only_report(raw_report) if args.markout_only else raw_report
        )

    _print_policy(policy, fee_model, fallback_schedule)
    if args.markout_only:
        print(
            "\nMARKOUT-ONLY MODE: unsettled markets included; cash P&L is"
            f" {CASH_PNL_UNAVAILABLE} on every block (never a number,"
            f" never 0.0); terminal={MARKOUT_ONLY_TERMINAL} (terminal"
            " inventory's settlement term is excluded from markout_pnl"
            " for every market -- module docstring)."
        )
    for fill_model in ("pessimistic", "optimistic"):
        _print_model(payload[fill_model])
        if args.markout_only:
            for name in ("overall", "train", "test"):
                block = payload[fill_model][name]
                per_fill = block["mean_markout_pnl_per_fill"]
                per_fill_str = "n/a (no fills)" if per_fill is None else f"{per_fill:+.4f}"
                print(
                    f"      {name} per-fill markout edge: {per_fill_str}"
                    f" (total_markout_pnl={block['total_markout_pnl']:+.4f},"
                    f" n_fills={block['n_fills']})"
                )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, allow_nan=False)
        print(f"\nwrote {args.out}")
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    sys.exit(main())
