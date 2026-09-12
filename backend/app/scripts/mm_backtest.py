"""Replay `MarketMaker` through `PassiveFillEngine` over settled history.

WHAT THIS IS. The Gate 1 harness (mm-proveout T2, PLAN.md D2/D11): the
one place a passive-quoting P&L number is produced in this repo. The
preceding session ran this pipeline out of scratchpad scripts that were
never committed; every number in `app/strategies/market_making.py`'s
calibration tables came out of them, and none of it could be reproduced.
This module is those scripts, made importable and testable.

`replay()` and `report()` are the shared surface PLAN.md D11 names: the
Polymarket forward replay (T11) imports them so both venues are scored
by byte-identical logic, and the calibration sweep (T4), the minute
study (T5) and the taper (T6) all run through them too. Hence the
venue-agnostic `MarketCandles` interchange type, the injectable
`fee_model`/`schedule`, and the fact that nothing below reaches for a
Kalshi adapter except `settled_universe`/`collect`.

THE ACCOUNTING, precisely, because it is the whole result.

For each market, for each candle index `i` with `i`, `i+1`, `i+2`
available:

  * QUOTE from candle `i`'s `bid_close`/`ask_close`. That candle has
    already CLOSED (T1's `Candle.end_ts` is the period END), so its
    quote was observable when the order would have been placed.
  * FILL against candle `i+1`'s `px_low`/`px_high`/`volume` via
    `TradeRange` -- the prints that happened while the order rested.
  * MARK at candle `i+2`'s mid. One full interval AFTER the interval the
    fill happened in, never inside it (GUARDRAILS.md §4.3; PLAN.md
    Risks, "Look-ahead through the mark"). Marking at `i+1` would score
    the trade against a price the fill itself helped set.

TWO P&L NUMBERS, BECAUSE THEY ANSWER TWO QUESTIONS. Every `MarketRow`
and every numeric block of `report()` carries both, and neither is a
substitute for the other.

`pnl` IS THE MONEY -- cash settled, and the only figure the confidence
interval, `roc` and the Gate 1 verdict are computed on:

    pnl = sum over fills of (-direction * size * price)
        - sum over fills of fee
        + terminal_inventory * settle

where `direction` is +1 for a buy and -1 for a sell (so a buy pays out
cash and a sell takes it in), and `settle` is 1.0 for a market that
resolved `"yes"` and 0.0 for `"no"` (GUARDRAILS.md §4.2, PLAN.md D4:
terminal inventory is SETTLED at the venue's real result, never marked).
No mark appears in it anywhere. That is the point: what a quoter
actually banked cannot depend on which candles a scorer happened to pick
as intermediate marks, and computing it as cash rather than as a
telescoping series of marks makes the independence structural instead of
a cancellation that has to be trusted.

`markout_pnl` IS THE QUOTE-QUALITY STATISTIC -- each fill marked ONCE at
its own `mid(i+2)`, plus the same settlement term applied from the last
mark the inventory was valued at:

    markout_pnl = sum over fills of mark_to_market(fill, mid(i+2))
                + terminal_inventory * (settle - last_mid)

Scoring each fill against fair value one interval later measures whether
the quote was picked off, which is the question `passive_fill.py` exists
to ask (+0.0051 optimistic vs -0.0095 pessimistic on the same 34,137
candles). It is mark-DEPENDENT by construction, and "the mark is i+2,
not i+1" is a genuine property of it, which is why
`tests/scripts/test_mm_backtest.py::test_the_mark_is_two_candles_ahead
_not_one` asserts on this number and not on `pnl`.

THE GAP BETWEEN THEM is exactly

    sum over intervals of inventory_before_interval * (mark_i - mark_prev)

-- the drift on inventory carried BETWEEN fills. `markout_pnl` omits
that term; `pnl` contains it, because the money does.

WHY THIS IS SPLIT IN TWO AND WAS NOT ALWAYS (the correction, recorded
because the earlier convention shipped and produced numbers). This
module originally reported ONLY the marked number, as `pnl`, and
defended the omission as having "zero expectation under a martingale
price". Both halves of that defence fail:

  * The martingale premise is the one thing this project has already
    measured false. Maker fills here are adversely selected -- sell
    fills outnumber buy fills 1.5-2.0x at every price
    (`market_making.py`), which is what "adverse selection = quoted
    minus realized half-spread" measures in `passive_fill.py`'s
    surrounding work. Flow that picks the maker off is not a martingale
    conditional on the fill, and the drift's sign is against the maker.
  * Even granting zero expectation, the term is pure ADDED VARIANCE on
    a statistic whose Gate 1 verdict (T3) is `ci_low > 0` on a
    clustered CI. Zero-mean noise on every market's P&L widens every
    interval and pushes the gate toward NO-GO regardless of the truth.
    The preceding session measured +$0.15-0.26 per market with a CI
    already spanning zero; this convention was a live candidate for
    why.

The concrete case, pinned as a regression by
`test_a_multi_fill_position_diverges_from_cash_settled_pnl_by_a_real
_amount`: buy 10 @ 0.34, sell 10 @ 0.67, sell 10 @ 0.67 again, ending
10 short into a `"yes"` result. Real money is `-3.40 + 13.40 - 10.00 =
$0.00` exactly, before fees. The marked number is -$2.45, which reduces
algebraically to `10 * (m1 - m2 - m3 + m_last)` -- a function of which
candles happened to be the marks, not of the trades. Reporting that as
`pnl` reported a loss on a market that broke even.

Both numbers are computed over the SAME fill set, so they are
comparable: an interval whose `i+2` carries no two-sided book yields no
mark, and the replay takes no fills in it at all (counted as
`n_unmarkable`) rather than taking a fill it could only score in one of
the two conventions.

COLLATERAL, and therefore `roc`. A resting two-sided quote ties up
`bid.price * size` on the buy (cash to pay if filled) and
`(1 - ask.price) * size` on the sell (a short contract's collateral is
what it pays out if YES resolves). Accrued per QUOTED hour only: an
interval the policy refused to quote locks nothing, and counting it
would understate the capital intensity of the strategy.

LABELS ARE NOT DECORATION (GUARDRAILS.md §2.1). Every numeric block this
module emits, in JSON and on stdout, carries `fill_model` and
`terminal="settled"`; pessimistic is reported first and the verdict is
computed on it (§2.2). The maker rebate is accumulated but NEVER added
to P&L -- it is reported as its own "would add $X if paid as published"
line (§2.3, PLAN.md D6), and `FeeSchedule.maker_rebate_rate` reaching
`FeeModel.fee()` is precisely the leak that would let projected revenue
into every cost figure in the repo.

READ-ONLY (GUARDRAILS.md §1.1/§1.3). The only venue traffic is
`KalshiAdapter.list_markets` and the read-only candlestick `GET`s T1's
`fetch_candles` makes. This module has no code path that could place,
modify or cancel an order, and it never opens `.env` -- the adapter
loads it.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import random
import statistics
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx

from app.execution.passive_fill import (
    FillModel,
    PassiveFill,
    PassiveFillEngine,
    TradeRange,
    mark_to_market,
)
from app.logging_config import configure_logging
from app.scripts.calibration import cluster_bootstrap
from app.strategies.market_making import (
    DEFAULT_EDGE_FRACTION,
    DEFAULT_MAX_INVENTORY,
    DEFAULT_MIN_SPREAD,
    DEFAULT_SKEW_STRENGTH,
    MarketMaker,
    QuotePair,
)
from app.venues.base import FeeModel, VenueError, VenuePayloadError
from app.venues.fees import KalshiFeeModel, default_kalshi_schedule
from app.venues.kalshi.adapter import KalshiAdapter
from app.venues.kalshi.candles import Candle, fetch_candles, series_for
from app.venues.types import (
    BookLevel,
    FeeSchedule,
    OrderBook,
    VenueId,
    VenueMarket,
    venue_volume,
)

#: How a split was drawn. `"temporal"` (earlier closes -> later closes)
#: is the go/no-go split; `"event"` (whole events to one side or the
#: other) is for parameter tuning only. PLAN.md D5, GUARDRAILS.md §4.1:
#: a report that tunes and scores on the same split is not out of sample.
SplitKind = Literal["event", "temporal"]

#: The only `terminal` value this kit's numbers may carry
#: (GUARDRAILS.md §2.1). Terminal inventory is settled at the venue's
#: real result, never marked, so there is no second value to choose.
TERMINAL = "settled"

#: `result` values that carry a settlement price. Anything else
#: (`"scalar"`, `""`, `None`) is excluded from P&L and COUNTED as
#: survivorship (GUARDRAILS.md §4.2).
SETTLEABLE_RESULTS: frozenset[str] = frozenset({"yes", "no"})

#: Minimum traded volume for a settled market to enter the universe.
#: Measured 2026-09-07: 50,704 settled Kalshi markets clear 2,000, with
#: closes spanning 2026-06-30 -> 2026-09-07 (PLAN.md §Verified facts).
#: Below this a market's "quote" is one or two prints and carries no
#: information about what a resting order would have met.
DEFAULT_MIN_VOLUME = 2000.0

#: Candle granularity in minutes. Hourly is the interval every measured
#: number in `market_making.py` was calibrated on (34,137 hourly candles
#: across 537 markets); T5 studies 1-minute separately.
DEFAULT_INTERVAL_MINUTES = 60

#: How far back to request candles from each market's close. Ten days of
#: hourly candles is ~240 intervals per market -- enough for the quote/
#: fill/mark triple to repeat, without paying for history the policy
#: would not have quoted into anyway.
DEFAULT_LOOKBACK_DAYS = 10

#: Concurrent candle fetches. Measured 2026-09-07: 700 markets in 134s
#: at this concurrency with ZERO 429s, against the adapter's own pacing
#: (`kalshi_min_request_interval_s`). PLAN.md Risks: if >1% of requests
#: 429 at scale, HALVE this -- never disable pacing.
DEFAULT_CONCURRENCY = 8

#: A market needs at least this many candles to contribute anything: the
#: replay consumes them in (quote, fill, mark) triples, so three is the
#: bare minimum and four is the first count that produces more than one
#: quote-hour. Fewer is not an error, it is a market with no history.
MIN_CANDLES = 4

#: Markets per `asyncio.gather` batch. Mirrors `calibration.py`: bounds
#: peak memory and the number of in-flight tasks independently of the
#: semaphore that bounds actual concurrency.
COLLECT_BATCH = 250

#: Markets between `--cache` flushes during collection (Change 2,
#: 2026-09-07: three live runs crashed at samples 8,000/20,000/19,000
#: and lost everything because `write_cache` only ran once, at the end).
#: Fixed at 500 by mandate, not derived or tuned here: on a 20,000-market
#: run that is 40 writes, negligible against a ~30-minute collection.
#: Deliberately not tied to `COLLECT_BATCH` (250) -- one bounds
#: concurrency, the other bounds data loss on a crash, and coupling them
#: would let a change to either silently retune the other.
CACHE_FLUSH_EVERY = 500

#: Cluster-bootstrap replicates for the 95% interval, and resampling
#: replicates for the power table. 500 is `calibration.py`'s value.
BOOTSTRAP_REPLICATES = 500

#: Seed for every draw this module makes, so a report is reproducible
#: from its `--seed` alone.
DEFAULT_SEED = 20260906

#: Portfolio sizes the power table reports. The preceding session put
#: the P(profit) > 99% threshold at ~2,500 simultaneous markets; the
#: table brackets that so the reader can see where it crosses rather
#: than being handed one number.
PORTFOLIO_SIZES: tuple[int, ...] = (500, 1000, 2500, 5000)

#: Distinct EVENTS `cluster_bootstrap` (`app.scripts.calibration`)
#: requires before it returns an interval at all; below this it hands
#: back `(nan, nan)`. Mirrored here as a named constant because
#: `MIN_POWER_POOL_EVENTS` is defined in terms of it, and a hand-copied
#: literal would silently drift if calibration's changed.
#: `tests/scripts/test_mm_backtest.py::test_the_power_table_never_speaks
#: _where_the_confidence_interval_refuses` calls the real
#: `cluster_bootstrap` on both sides of this number rather than trusting
#: it.
CI_FLOOR_EVENTS = 5

#: Distinct trading EVENTS the power table's resample pool must contain
#: before it will print a `p_profit` or a 5th percentile at all.
#:
#: WHY THERE IS A FLOOR. `_power_table` builds a portfolio by drawing
#: WITH REPLACEMENT from the pool, so `p_profit` is forced arithmetic
#: whenever the pool has no negative value: every replicate's total is
#: positive at EVERY portfolio size, and the table prints 1.0000. That is
#: not a measurement of a portfolio, it is a restatement of the pool's
#: sign. Reproduced live at `--sample 30`, where a test half of four
#: trading markets printed `P(profit) 1.0000` at 500 through 5000
#: directly beneath a verdict that correctly reported its CI as
#: unavailable -- two blocks of the same report disagreeing about
#: whether there was any evidence.
#:
#: WHY THE UNIT IS EVENTS, NOT MARKETS (the correction; the first
#: version of this floor counted markets and shipped). A market-count
#: floor refuses a SMALL pool and does nothing about a CONCENTRATED one.
#: Forty markets that all belong to one event cleared a 30-MARKET floor
#: while `cluster_bootstrap` beside it correctly refused at one event:
#: `ci95_clustered_by_event [None, None]` and `p_profit 1.0` at
#: portfolio 5000 in the same JSON object. The floor's own `p**n`
#: argument assumes n INDEPENDENT draws, and nothing enforced that.
#: The concentration is live, not hypothetical: measured 2026-09-07,
#: `settled_universe(min_volume=2000)` returns 50,796 markets across
#: 18,457 events, the largest being `KXDPWORLDTOUR-OMEM26` (155),
#: `KXWORLDCUPHALFTIME-26` (130), `KXDPWORLDTOURR1LEAD-OMEM26` (74),
#: `KXPGATOP20-BMC26` (50) and `KXBTCD-26SEP0417` (49) -- golf brackets,
#: halftime prop families and one day's BTC threshold ladder, each ONE
#: correlated real-world outcome. All 49 of `KXBTCD-26SEP0417` land
#: entirely in the TEST split at the harness's own median-close default
#: cutoff, which is the split the verdict and this table print from.
#:
#: WHY 30. Two reasons, and the larger governs.
#:
#:   * It must exceed what the CI already refuses. `cluster_bootstrap`
#:     returns no interval below `CI_FLOOR_EVENTS` distinct events, and
#:     the power table assumes strictly MORE than the CI does (it draws
#:     whole events as independent units, where the CI only resamples
#:     the ones it was given), so it cannot honestly speak where the CI
#:     declines to. 30 clears 5 six times over.
#:   * The forced-1.0000 artifact has to become unlikely rather than
#:     merely possible. If an EVENT's summed P&L is positive with
#:     probability p, an all-positive pool of n events has probability
#:     p**n -- and now n really is a count of independent draws, which
#:     is what makes the arithmetic apply. At the preceding session's
#:     per-market Sharpe of ~0.05 the plausible p is around 0.8 (the
#:     strategy's left tail is the short-into-yes settlement, not a
#:     symmetric spread); 0.8**6 = 0.26, so a six-event pool shows no
#:     loser a quarter of the time by luck alone, while 0.8**30 =
#:     0.0012. Thirty is the smallest round number at which "this pool
#:     contains no losing event" is itself evidence rather than an
#:     accident of size.
#:
#: A market-count floor is now redundant rather than removed: a pool of
#: 30 distinct events holds at least 30 markets by construction.
#:
#: This is a REFUSAL threshold, not a selection threshold: raising it
#: cannot manufacture sample size and lowering it cannot make a verdict
#: (GUARDRAILS.md §2.6). Below it the block reports `null` and says so.
#: It is NOT the guard that keeps the resampler off an empty pool --
#: that is a separate, explicit check inside `_power_table`, so that
#: retuning this number for statistical reasons cannot change crash
#: behaviour.
MIN_POWER_POOL_EVENTS = 30

#: WHERE `n_universe` COMES FROM, AND WHAT IT IS NOT (GUARDRAILS.md §7:
#: no number without its provenance).
#:
#: `settled_universe` reads `KalshiAdapter.list_markets(status=
#: "resolved")`, which walks `/events?status=settled` under a hard cap of
#: `_MAX_EVENT_PAGES = 150` pages (`app/venues/kalshi/adapter.py`). The
#: listing does not exhaust at that cap -- measured 2026-09-07 by walking
#: it past the cap, it had not exhausted at 400 pages / 1,241,961
#: markets. So `n_universe` counts the VISIBLE settled listing, not
#: Kalshi's settled universe, and the sample drawn from it is not a
#: random draw from the venue.
#:
#: The measurement, over tradeable settled markets (`result` in
#: yes/no, `volume_fp` > 2000):
#:
#:   month     inside the cap   beyond it (invisible)
#:   2026-07            2,125                     480
#:   2026-08            4,613                  77,602
#:   2026-09           44,087                   5,069
#:   total             50,826                  83,151
#:
#: 38% visible, August 94% missing -- and the listing is NOT
#: chronologically ordered (page 0 spans 2025-2031), so this is an
#: uneven, month-correlated bias rather than a clean missing recent
#: tail. Raising the cap is not the fix: any cap truncates a listing
#: that exceeds 400 pages.
#:
#: WHY THIS IS A FIELD AND NOT A FOOTNOTE. `survivorship_share` beside
#: it reports something else entirely -- exclusion by a non-binary
#: `result` -- and T3's brief tells its author to copy "the survivorship
#: count" out of this JSON. With nothing to hang this caveat on, it
#: survives only if that author independently reads NOTES.md, which is
#: exactly the hand-off that fails. So it travels IN the report, in the
#: JSON and in the printed header.
#:
#: What it does and does not damage: a temporal split WITHIN the visible
#: set is still internally valid -- it holds out later closes from
#: earlier ones on data actually observed. What is damaged is EXTERNAL
#: validity: a Gate 1 verdict generalises to "markets like the ones the
#: listing shows", not to "Kalshi".
SETTLED_LISTING_PROVENANCE: dict[str, Any] = {
    "listing_is_page_capped": True,
    "cap": (
        "_MAX_EVENT_PAGES=150 pages x _EVENTS_PAGE_LIMIT=200 events/page "
        "in app/venues/kalshi/adapter.py; the listing did not exhaust at "
        "400 pages / 1,241,961 markets when walked past the cap"
    ),
    "sample_is_random_draw_from_venue": False,
    "measured_on": "2026-09-07",
    "visible_tradeable_markets": 50_826,
    "invisible_tradeable_markets": 83_151,
    "visible_share_of_tradeable_universe": 50_826 / (50_826 + 83_151),
    "bias": (
        "month-correlated, not random. Tradeable settled markets inside "
        "the cap vs beyond it: 2026-07 2,125/480; 2026-08 4,613/77,602 "
        "(94% missing); 2026-09 44,087/5,069. The listing is not "
        "chronologically ordered, so this is not a missing recent tail."
    ),
    "consequence": (
        "n_universe counts the VISIBLE settled listing, so n is not a "
        "random draw from the venue and a verdict computed on it "
        "generalises to 'markets like the ones the listing shows', not "
        "to Kalshi. Distinct from survivorship_share, which counts only "
        "exclusion by a non-binary result. A temporal split within the "
        "visible set remains internally valid."
    ),
}

#: Share of requested markets that may fail to collect before the whole
#: run is treated as broken rather than merely lossy. One bad market in
#: 8,000 is a market; 2% of them is the venue's payload shape having
#: changed under T1's parser, and reporting that run as clean is the
#: defect this threshold exists to prevent.
MAX_COLLECT_FAILURE_RATE = 0.02

#: Size stamped on the synthetic book levels handed to `MarketMaker.
#: quote`. A candle reports the closing best bid/ask PRICE and NO size,
#: so `0.0` is the honest encoding of "depth not observed" -- inventing
#: a size here would be a depth claim, and `synthesize_book`'s
#: `depth_source="synthetic"` labelling exists precisely because those
#: must never pass unlabelled (market-edge GUARDRAILS.md §1.7). Nothing
#: in this module reads it: `MarketMaker.quote` reads only
#: `best_bid().price`/`best_ask().price`, and fills come from the trade
#: tape (`TradeRange`), never from walking this book.
_BOOK_LEVEL_SIZE = 0.0

#: `MarketMaker.__init__`'s own default for `quote_size`. The strategy
#: module exports named constants for its other four parameters but not
#: this one, so the CLI's default is pinned here rather than silently
#: diverging from the policy's.
DEFAULT_QUOTE_SIZE = 10.0

#: Bounded retry for `settled_universe`'s OWN `list_markets` call (Change
#: 3, 2026-09-07). Distinct from `_collect_one`'s per-market candle-fetch
#: catch (Change 1, above): that guards a LATER, per-market call made
#: only once collection has already started. This one guards the walk
#: that runs BEFORE collection starts at all -- up to
#: `_MAX_EVENT_PAGES=150` sequential `/events` pages
#: (`app/venues/kalshi/adapter.py::_fetch_event_markets`) -- which had NO
#: transport-error handling anywhere on it and crashed a live run ~80
#: minutes in on one dropped TLS read, discarding the entire walk before
#: the run printed a `universe:` line or wrote anything at all.
_UNIVERSE_LIST_ATTEMPTS = 3

#: Fixed backoff between `_UNIVERSE_LIST_ATTEMPTS` attempts. Short and
#: NOT exponential, unlike `KalshiAdapter._get`'s own 429 backoff: this
#: guards a dropped connection, not a rate limit, and a fresh TCP
#: handshake on the next attempt is normally all a transient read
#: failure needs. Tests monkeypatch this to `0` rather than paying it
#: for real.
_UNIVERSE_LIST_BACKOFF_S = 2.0


# ---------------------------------------------------------------------------
# Interchange types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketCandles:
    """One settled market and the history the replay consumes.

    The venue-agnostic hand-off between collection and `replay()`
    (PLAN.md D11): Kalshi builds these from `fetch_candles`, and T11's
    Polymarket replay builds them from stored `BookSnapshot` rows, so
    both venues are scored by the same `replay()`/`report()` code rather
    than by two implementations that drift.

    Attributes:
        venue: Venue this market trades on.
        market_id: Venue-native market identifier.
        event: Event key the market belongs to. Every interval this kit
            reports is clustered on it (GUARDRAILS.md §2.4), so it is
            required, not optional -- a market with no event falls back
            to its own id (a cluster of one) at construction time,
            never silently to a shared empty string.
        series: Venue series/family the market belongs to, `""` where
            the venue has no such concept.
        close_ts: Unix timestamp (seconds) trading closed. The temporal
            split is drawn on this.
        result: The venue's settled result. Must be in
            `SETTLEABLE_RESULTS` -- see `__post_init__`.
        candles: History ascending by `end_ts`.
        fee: The fee schedule in force for this market, or `None` to use
            the replay's default. PLAN.md D9: schedules change per
            market over time, so the one in force belongs on the
            observation rather than being assumed at scoring time.
        outcome: Outcome being quoted.
    """

    venue: VenueId
    market_id: str
    event: str
    series: str
    close_ts: int
    result: str
    candles: tuple[Candle, ...]
    fee: FeeSchedule | None = None
    outcome: str = "YES"

    def __post_init__(self) -> None:
        """Refuse a market that cannot be settled, and freeze `candles`.

        A market whose `result` is not `"yes"`/`"no"` has no settlement
        price, and there is no safe value to invent for one: scoring its
        terminal inventory at a MARK is the exact substitution PLAN.md
        D4 forbids (it "turned a null result into a significant one").
        `settled_universe` already excludes and counts these, so this
        raise is the second fence rather than the first -- it exists so
        no future caller can route around the count.

        Raises:
            ValueError: If `result` is not in `SETTLEABLE_RESULTS`.
        """
        if self.result not in SETTLEABLE_RESULTS:
            raise ValueError(
                f"result must be one of {sorted(SETTLEABLE_RESULTS)} to be "
                f"settled, got {self.result!r} for {self.market_id!r}"
            )
        object.__setattr__(self, "candles", tuple(self.candles))
        if not self.event:
            object.__setattr__(self, "event", self.market_id)

    @property
    def settle(self) -> float:
        """The settlement value of one YES contract, 1.0 or 0.0."""
        return 1.0 if self.result == "yes" else 0.0


@dataclass(frozen=True)
class MarketRow:
    """What one market contributed to the replay.

    Attributes:
        market_id: Venue-native market identifier.
        event: Cluster key for the bootstrap (GUARDRAILS.md §2.4).
        series: Venue series/family.
        close_ts: Unix timestamp trading closed; the temporal split key.
        quote_hours: Intervals the policy actually rested a quote in.
            Zero means the policy looked at this market and declined it,
            which is an answer, not a gap.
        n_fills: Passive fills taken, both sides counted.
        n_two_sided: Quote-hours where BOTH sides rested.
        pnl: THE MONEY, in USD: cash paid and received on every fill,
            less fees, plus terminal inventory settled at `result`. No
            mark enters it (module docstring, "TWO P&L NUMBERS"). This
            is what the confidence interval, `roc` and the Gate 1
            verdict are computed on. The rebate is NEVER added.
        markout_pnl: The quote-quality statistic, in USD: each fill
            marked once at its own `mid(i+2)`, plus terminal inventory
            settled from `last_mid`. Mark-DEPENDENT on purpose -- it
            measures whether the quotes were picked off, independent of
            where the price happened to finish. Never the money, and
            never the basis of a verdict.
        collateral_mean: Mean USD locked per QUOTED hour, `0.0` when
            nothing was quoted.
        terminal_inventory: Signed contracts held when the history ran
            out; positive long, negative short.
        held_into_settlement: Whether `terminal_inventory` was non-zero.
        settled_short_into_yes: Whether a SHORT position settled into a
            `"yes"` result -- the tail this strategy accumulates by
            construction, since sell fills outnumber buy fills 1.5-2.0x
            at every price (`market_making.py`).
        rebate_if_paid: USD the venue's published maker rebate WOULD
            have added, if paid as published. Reported beside P&L and
            never inside it (GUARDRAILS.md §2.3).
        last_mid: The mark `markout_pnl`'s terminal inventory term was
            settled from, or `None` if the market never filled. `pnl`
            does not use it.
    """

    market_id: str
    event: str
    series: str
    close_ts: int
    quote_hours: int
    n_fills: int
    n_two_sided: int
    pnl: float
    markout_pnl: float
    collateral_mean: float
    terminal_inventory: float
    held_into_settlement: bool
    settled_short_into_yes: bool
    rebate_if_paid: float
    last_mid: float | None


@dataclass(frozen=True)
class ReplayResult:
    """Every market's contribution under ONE fill model.

    Attributes:
        fill_model: The queue assumption every row was produced under.
            A `ReplayResult` never mixes two: the CLI runs `replay`
            twice, because the two models differ in SIGN on the same
            data (PLAN.md D3).
        rows: One row per input market, in input order -- including
            markets the policy never quoted.
        terminal: Always `TERMINAL`. Carried so a caller holding only
            this object can label its own output correctly.
        n_intervals: Quote/fill/mark triples examined across all
            markets, quoted or not.
        n_unmarkable: Triples skipped because candle `i+2` carried no
            two-sided book to take a mid from. Counted rather than
            silently dropped: a large number here means the mark, not
            the policy, is what the sample is short of.
    """

    fill_model: FillModel
    rows: tuple[MarketRow, ...]
    terminal: str = TERMINAL
    n_intervals: int = 0
    n_unmarkable: int = 0


@dataclass(frozen=True)
class CollectFailure:
    """One market that could not be collected, and why.

    Attributes:
        market_id: The market that failed.
        kind: `"payload"` when the venue's response could not be
            honoured (T1's `VenuePayloadError`), `"request"` for a
            transport/venue error. Kept apart because they mean opposite
            things: a wall of `"payload"` says the payload shape moved
            under the parser, a wall of `"request"` says the network or
            the rate limiter did.
        detail: The exception's message, truncated.
    """

    market_id: str
    kind: Literal["payload", "request"]
    detail: str


@dataclass(frozen=True)
class CollectResult:
    """The outcome of a collection run, INCLUDING what did not survive.

    WHY THE FAILURE COUNTS ARE A FIRST-CLASS FIELD (T1 carry-forward).
    T1 made `fetch_candles` strict on purpose: a corrupt payload raises
    rather than reading as an empty window, because "silently shortened
    time series" is a worse failure for a backtest than a raise. A
    collection loop that wrapped it in a bare `except: continue` would
    reinstate that exact defect one level up, where it is HARDER to see
    -- the market would simply not appear in the sample and nothing
    would say so.

    Aborting an 8,000-market run because one market's payload is odd is
    equally wrong. So `collect()` does neither: it isolates the failure
    to the one market, records it here with its kind, and lets the
    caller decide. `failure_rate` is what the caller decides on, and
    `summary()` puts every count into the report JSON -- a failure that
    never reaches the report is a failure nobody sees.

    Attributes:
        markets: Markets collected with usable history.
        failures: Per-market failures, capped for reporting.
        n_requested: Markets collection was asked for.
        n_short_history: Markets with fewer than `MIN_CANDLES` candles.
            NOT a failure: it says the market barely traded, not that
            the payload was dishonoured.
        n_payload_errors: Markets whose candles would not parse.
        n_request_errors: Markets whose fetch errored in transport.
        interval_minutes: Candle granularity collected at.
        lookback_days: Days of history requested before each close.
        n_universe: Size of the volume-qualified settled universe this
            run sampled from, and `excluded_by_result` the survivorship
            count beside it. `collect()` cannot know either -- they come
            from `settled_universe` -- so the CLI stamps them on with
            `dataclasses.replace` before caching. They live HERE rather
            than only in the report because a run read back from
            `--cache` must state the same universe and the same
            survivorship as the run that produced it; a cached run that
            reported "universe: 27, excluded by result: 0" would be
            quoting a figure it never measured.
        excluded_by_result: See `n_universe`.
    """

    markets: list[MarketCandles]
    failures: list[CollectFailure]
    n_requested: int
    n_short_history: int
    n_payload_errors: int
    n_request_errors: int
    interval_minutes: int
    lookback_days: int
    n_universe: int = 0
    excluded_by_result: int = 0

    @property
    def n_failed(self) -> int:
        """Markets lost to a payload or request error."""
        return self.n_payload_errors + self.n_request_errors

    @property
    def failure_rate(self) -> float:
        """Share of requested markets lost to an error, `0.0` if none."""
        if self.n_requested <= 0:
            return 0.0
        return self.n_failed / self.n_requested

    def summary(
        self,
        *,
        excluded_by_result: int | None = None,
        n_universe: int | None = None,
    ) -> dict[str, Any]:
        """The collection block as it appears in the report JSON.

        Args:
            excluded_by_result: Override for the field of the same
                name -- settled markets dropped for a `result` outside
                `SETTLEABLE_RESULTS` (`settled_universe`'s second return
                value), the survivorship count PLAN.md §Risks requires
                the report to state. `None` uses the stored field.
            n_universe: Override for the field of the same name; `None`
                uses the stored field.

        Returns:
            dict[str, Any]: Counts only; no P&L figure appears here, so
                this block carries no `fill_model`/`terminal` label and
                needs none. It DOES carry `universe_provenance`
                (`SETTLED_LISTING_PROVENANCE`), because `n_universe` is
                a count of a page-capped listing and a truncated number
                travelling without its truncation is the defect
                GUARDRAILS.md §7 names.
        """
        excluded = (
            self.excluded_by_result
            if excluded_by_result is None
            else excluded_by_result
        )
        universe = self.n_universe if n_universe is None else n_universe
        return {
            "n_universe": universe,
            "excluded_by_result": excluded,
            "survivorship_share": (
                excluded / (excluded + universe)
                if excluded + universe > 0
                else 0.0
            ),
            # `n_universe` is a truncated count and must never travel
            # without saying so (GUARDRAILS.md §7). Copied rather than
            # shared, so a consumer that mutates its report cannot
            # rewrite the module's record of the measurement.
            "universe_provenance": dict(SETTLED_LISTING_PROVENANCE),
            "n_requested": self.n_requested,
            "n_collected": len(self.markets),
            "n_short_history": self.n_short_history,
            "n_payload_errors": self.n_payload_errors,
            "n_request_errors": self.n_request_errors,
            "failure_rate": self.failure_rate,
            "interval_minutes": self.interval_minutes,
            "lookback_days": self.lookback_days,
            "failure_sample": [
                {"market_id": f.market_id, "kind": f.kind, "detail": f.detail}
                for f in self.failures[:20]
            ],
        }


# ---------------------------------------------------------------------------
# 1. The universe
# ---------------------------------------------------------------------------


def _float(value: object) -> float | None:
    """Best-effort finite `float(value)`; `None` on failure."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


async def settled_universe(
    adapter: Any, min_volume: float = DEFAULT_MIN_VOLUME
) -> tuple[list[VenueMarket], int]:
    """Every settled market with a binary result and real volume.

    Volume is read from `venue_volume` (which prefers `volume_24h_fp`)
    OR the market's lifetime `volume_fp`, whichever clears the floor: a
    settled market's 24h volume is usually zero by the time it is
    listed, so requiring `venue_volume` alone would empty the universe,
    while requiring `volume_fp` alone would drop markets whose lifetime
    figure the payload omits. Both field names are the venue's own
    (GUARDRAILS.md §3.2), never a bare `volume`.

    SURVIVORSHIP IS COUNTED, NOT ASSUMED AWAY (PLAN.md §Risks,
    GUARDRAILS.md §4.2). A market that settled to something other than
    `"yes"`/`"no"` -- a scalar market, a void, a delisting -- has no
    settlement price for terminal inventory, so it cannot enter the
    replay. The count of those is returned alongside the universe so the
    report can state the share, rather than the exclusion happening
    invisibly. The count is taken among markets that ALREADY cleared the
    volume floor, so it is a share of the sample actually studied; a
    market dropped for thin volume says nothing about settlement and is
    not counted here.

    THE WALK RETRIES A TRANSPORT BLIP; IT NEVER RETRIES A BAD PAYLOAD
    (Change 3, 2026-09-07). `list_markets(status="resolved")` is one
    sequential walk of up to `_MAX_EVENT_PAGES=150` `/events` pages
    (`KalshiAdapter._fetch_event_markets`), and until this change nothing
    on that path retried a dropped connection: a single
    `httpx.ReadError` anywhere in it discarded the ENTIRE walk and, on a
    live run, crashed the process ~80 minutes in -- before it ever
    printed a `universe:` line or wrote anything. The call below is now
    retried up to `_UNIVERSE_LIST_ATTEMPTS` times on `httpx.HTTPError`,
    which covers both `httpx.TransportError` (the connection dropped
    mid-request) and `httpx.HTTPStatusError` (`raise_for_venue_error`'s
    fallthrough for a non-2xx status Kalshi did not map to a
    `VenueError`), with a fixed backoff between attempts.
    `VenuePayloadError` is deliberately EXCLUDED: a payload the parser
    cannot honour means our parsing is wrong or the venue's contract
    changed, and retrying that risks quietly succeeding into a wrong
    answer instead of staying loud, so it propagates on the first
    occurrence exactly as before. Exhausting every attempt raises,
    naming how many were made, so the failure still says what happened
    instead of trailing off into whatever `httpx`'s own message says.

    Args:
        adapter: Anything exposing `list_markets(status=...)` -- the
            live `KalshiAdapter`, or a stub in tests.
        min_volume: Contracts floor, in either volume field.

    Returns:
        tuple[list[VenueMarket], int]: The qualifying markets, and the
            number excluded because their `result` is not settleable.

    Raises:
        httpx.HTTPError: If `list_markets` still raises a transport or
            status error after `_UNIVERSE_LIST_ATTEMPTS` attempts.
    """
    markets: list[VenueMarket] | None = None
    for attempt in range(1, _UNIVERSE_LIST_ATTEMPTS + 1):
        try:
            markets = await adapter.list_markets(status="resolved")
            break
        except httpx.HTTPError as exc:
            if attempt == _UNIVERSE_LIST_ATTEMPTS:
                raise httpx.HTTPError(
                    "settled_universe: list_markets(status='resolved') failed"
                    f" after {_UNIVERSE_LIST_ATTEMPTS} attempts: {exc}"
                ) from exc
            print(
                f"settled_universe: list_markets attempt {attempt}/"
                f"{_UNIVERSE_LIST_ATTEMPTS} failed ({exc!r}); retrying",
                file=sys.stderr,
            )
            await asyncio.sleep(_UNIVERSE_LIST_BACKOFF_S)
    assert markets is not None  # loop above always returns or raises
    universe: list[VenueMarket] = []
    excluded_by_result = 0
    for market in markets:
        lifetime = _float(market.raw.get("volume_fp")) or 0.0
        if venue_volume(market) < min_volume and lifetime < min_volume:
            continue
        if (market.result or "") not in SETTLEABLE_RESULTS:
            excluded_by_result += 1
            continue
        universe.append(market)
    return universe, excluded_by_result


def _venue_market_to_json(market: VenueMarket) -> dict[str, Any]:
    """One `VenueMarket` as a JSON-safe dict (the universe cache contract).

    Every field `VenueMarket` carries round-trips -- `raw` included, so a
    market loaded from this cache is indistinguishable from one just
    listed, and everything downstream (`venue_volume`, `series_for`,
    fee lookups) still works from `raw` exactly as it does on a fresh
    walk. `close_time`/`expected_settle_time` go through `isoformat()`,
    which always includes the UTC offset the venue types require;
    `_venue_market_from_json` reads it back with `datetime.fromisoformat`
    rather than assuming UTC.
    """
    return {
        "venue": market.venue,
        "market_id": market.market_id,
        "event_id": market.event_id,
        "question": market.question,
        "outcomes": list(market.outcomes),
        "outcome_ids": dict(market.outcome_ids),
        "rules_text": market.rules_text,
        "resolution_source": market.resolution_source,
        "close_time": market.close_time.isoformat(),
        "expected_settle_time": (
            None
            if market.expected_settle_time is None
            else market.expected_settle_time.isoformat()
        ),
        "status": market.status,
        "result": market.result,
        "tick_size": market.tick_size,
        "min_size": market.min_size,
        "fee": {
            "taker_rate": market.fee.taker_rate,
            "maker_rate": market.fee.maker_rate,
            "source": market.fee.source,
            "maker_rebate_rate": market.fee.maker_rebate_rate,
        },
        "raw": dict(market.raw),
    }


def _venue_market_from_json(row: dict[str, Any]) -> VenueMarket:
    """Inverse of `_venue_market_to_json`."""
    return VenueMarket(
        venue=row["venue"],
        market_id=row["market_id"],
        event_id=row.get("event_id"),
        question=row["question"],
        outcomes=tuple(row["outcomes"]),
        outcome_ids=dict(row["outcome_ids"]),
        rules_text=row["rules_text"],
        resolution_source=row.get("resolution_source"),
        close_time=datetime.fromisoformat(row["close_time"]),
        expected_settle_time=(
            None
            if row.get("expected_settle_time") is None
            else datetime.fromisoformat(row["expected_settle_time"])
        ),
        status=row["status"],
        result=row.get("result"),
        tick_size=row["tick_size"],
        min_size=row["min_size"],
        fee=FeeSchedule(
            taker_rate=row["fee"]["taker_rate"],
            maker_rate=row["fee"]["maker_rate"],
            source=row["fee"]["source"],
            maker_rebate_rate=row["fee"].get("maker_rebate_rate", 0.0),
        ),
        raw=row.get("raw", {}),
    )


@dataclass(frozen=True)
class UniverseCache:
    """A `settled_universe` result, cached beside the candle `--cache`
    (Change 3, 2026-09-07 -- "the real win": the walk is the single most
    expensive step this script makes and, until this change, was
    re-walked from scratch on every invocation, including every retry
    after a crash).

    Attributes:
        markets: The qualifying `VenueMarket`s `settled_universe`
            returned.
        excluded_by_result: The survivorship count `settled_universe`
            returned alongside them -- see its docstring. Carried here so
            a cached run reports the same survivorship as the run that
            produced it, exactly as `CollectResult.excluded_by_result`
            already does for the candle cache.
        min_volume: The floor the walk was filtered to -- the cache KEY.
            `_universe_or_load` compares this against the run's own
            `--min-volume` and refuses to serve a mismatch: a wider or
            narrower floor changes which markets are "in the universe",
            and serving one silently would be exactly the unlabelled
            truncation GUARDRAILS.md §7 forbids.
        collected_at: Aware UTC ISO timestamp the walk finished at.

            STALENESS IS REAL HERE AND DELIBERATELY NOT AUTO-EXPIRED.
            Markets keep settling after this timestamp, so a cached
            universe is exactly right for RE-RUNNING THE SAME
            EXPERIMENT -- that is the whole point of `--cache`, that a
            completed run's inputs do not silently change under it --
            and increasingly wrong as a measurement of "how many settled
            markets exist right now" the older it gets. Nothing in this
            module enforces a cutoff on that age: `_universe_or_load`
            prints it every time this cache is used, so the number is
            never hidden, and `--refresh-universe` is how a caller who
            wants a fresh measurement says so. Silently expiring the
            cache after some interval would substitute this module's
            guess about what counts as "too old" for the caller's own
            judgment about what their run needs.
    """

    markets: list[VenueMarket]
    excluded_by_result: int
    min_volume: float
    collected_at: str


def _universe_cache_path(cache_path: str | Path) -> Path:
    """Derive the universe cache's path from `--cache`.

    `<stem>.json` -> `<stem>.universe.json`, beside the candle cache, so
    one `--cache` flag controls both files and no second CLI flag is
    needed to name this one.
    """
    target = Path(cache_path)
    return target.with_name(f"{target.stem}.universe.json")


def write_universe_cache(path: str | Path, cache: UniverseCache) -> None:
    """Write `cache` to `path` as JSON.

    Same atomic `mkstemp` + `os.replace` contract as `write_cache`
    (below) -- see that function's docstring for why: a reader either
    sees the old complete file or the new one, never a partial write.
    """
    payload = {
        "min_volume": cache.min_volume,
        "collected_at": cache.collected_at,
        "excluded_by_result": cache.excluded_by_result,
        "markets": [_venue_market_to_json(m) for m in cache.markets],
    }
    target = Path(path)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, allow_nan=False)
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_name)
        raise


def read_universe_cache(path: str | Path) -> UniverseCache:
    """Read back a `write_universe_cache` file.

    Raises:
        FileNotFoundError: If `path` does not exist yet -- the caller
            treats that as "walk instead", exactly as `read_cache` does
            for the candle cache.
    """
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    return UniverseCache(
        markets=[_venue_market_from_json(row) for row in payload["markets"]],
        excluded_by_result=int(payload.get("excluded_by_result", 0)),
        min_volume=float(payload["min_volume"]),
        collected_at=str(payload["collected_at"]),
    )


async def _universe_or_load(
    adapter: Any,
    *,
    min_volume: float,
    cache_path: str | Path | None,
    refresh: bool,
) -> tuple[list[VenueMarket], int]:
    """`settled_universe`, or a `--cache`-derived cache of a previous walk.

    Three cases, mirroring `_collect_or_load`'s own:

    1. `cache_path` is `None` (no `--cache`): always walks. Universe
       caching piggybacks entirely on the candle `--cache` flag -- no
       separate flag exists to enable it on its own.
    2. A cache exists at `_universe_cache_path(cache_path)`, `refresh` is
       `False`, and its `min_volume` matches this run's: loaded, and its
       age is printed so staleness is never silent (see
       `UniverseCache.collected_at`).
    3. Anything else -- no cache file yet, `--refresh-universe`, or a
       `min_volume` mismatch: walks via `settled_universe` and writes
       the result back to `_universe_cache_path(cache_path)`.

    Prints which of these ran, so a reader of the log always knows
    whether the universe behind a report is fresh or reused.

    Args:
        adapter: Passed through to `settled_universe` on a walk.
        min_volume: This run's `--min-volume`; also the cache key.
        cache_path: This run's `--cache` value, or `None` to disable
            universe caching (mirrors the candle cache's own
            `None`-disables convention).
        refresh: `--refresh-universe` -- forces a walk even when a
            same-`min_volume` cache exists.

    Returns:
        tuple[list[VenueMarket], int]: Exactly `settled_universe`'s
            return shape, from whichever path produced it.
    """
    universe_path = None if not cache_path else _universe_cache_path(cache_path)
    if universe_path is not None and not refresh:
        cached: UniverseCache | None
        try:
            cached = read_universe_cache(universe_path)
        except FileNotFoundError:
            cached = None
        if cached is not None:
            if cached.min_volume != min_volume:
                print(
                    f"universe cache {universe_path} was collected at "
                    f"min_volume={cached.min_volume:.0f}; this run wants "
                    f"{min_volume:.0f} -- ignoring the cache and walking"
                )
            else:
                collected_at = datetime.fromisoformat(cached.collected_at)
                age = datetime.now(tz=UTC) - collected_at
                print(
                    f"universe: loaded from cache, collected_at="
                    f"{cached.collected_at} (age {age}), "
                    f"{len(cached.markets)} settled markets with volume >= "
                    f"{min_volume:.0f}; {cached.excluded_by_result} excluded "
                    "for a non-binary result"
                )
                return cached.markets, cached.excluded_by_result
    universe, excluded = await settled_universe(adapter, min_volume)
    print(
        f"universe: walked, {len(universe)} settled markets with volume >= "
        f"{min_volume:.0f}; {excluded} excluded for a non-binary result"
    )
    if universe_path is not None:
        write_universe_cache(
            universe_path,
            UniverseCache(
                markets=universe,
                excluded_by_result=excluded,
                min_volume=min_volume,
                collected_at=datetime.now(tz=UTC).isoformat(),
            ),
        )
    return universe, excluded


# ---------------------------------------------------------------------------
# 2. Collection
# ---------------------------------------------------------------------------


async def _collect_one(
    adapter: KalshiAdapter,
    market: VenueMarket,
    *,
    interval_minutes: int,
    lookback_days: int,
    semaphore: asyncio.Semaphore,
) -> MarketCandles | CollectFailure | None:
    """Fetch one market's candles.

    Returns:
        MarketCandles | CollectFailure | None: The market's history; a
            failure record if the fetch raised; or `None` if the market
            simply has fewer than `MIN_CANDLES` candles, which is not a
            failure (see `CollectResult`).
    """
    close = market.close_time
    async with semaphore:
        try:
            candles = await fetch_candles(
                adapter,
                series=series_for(market),
                ticker=market.market_id,
                start=close - timedelta(days=lookback_days),
                end=close,
                interval_minutes=interval_minutes,
            )
        except VenuePayloadError as exc:
            return CollectFailure(market.market_id, "payload", str(exc)[:300])
        except (VenueError, OSError, ValueError, httpx.HTTPError) as exc:
            # httpx.HTTPError covers httpx.TransportError (the network
            # died mid-request -- httpx.ReadError is one, and is NOT an
            # OSError, so it used to escape this tuple entirely and
            # abort collect() for every other market with it: three
            # consecutive live runs crashed this way, at samples
            # 8,000/20,000/19,000) and httpx.HTTPStatusError (the venue
            # answered with 4xx/5xx). Both mean "the request failed", the
            # same thing VenueError/OSError already mean here -- as
            # opposed to VenuePayloadError above, which means "the venue
            # answered but our parsing is wrong". A bare `except
            # Exception` would blur that distinction and swallow a bug
            # in this module's own code too, so it is deliberately not
            # used here.
            return CollectFailure(market.market_id, "request", str(exc)[:300])
    if len(candles) < MIN_CANDLES:
        return None
    return MarketCandles(
        venue=market.venue,
        market_id=market.market_id,
        event=market.event_id or market.market_id,
        series=series_for(market),
        close_ts=int(close.timestamp()),
        result=str(market.result),
        candles=tuple(candles),
        fee=market.fee,
    )


async def collect(
    adapter: KalshiAdapter,
    markets: Sequence[VenueMarket],
    *,
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> CollectResult:
    """Fetch candles for `markets`, isolating and COUNTING every failure.

    ONE MARKET'S BAD PAYLOAD IS NOT THE RUN'S PROBLEM, AND IS NOT
    NOTHING. T1 deliberately made `fetch_candles` raise on a payload it
    cannot honour, replacing a silent "corrupt reads as zero candles"
    defect. This function must not undo that at a higher level: a bare
    skip would hide the same failure in a place with even less
    visibility, and an abort would throw away 8,000 good markets for one
    bad one. So each market's failure is caught AT THAT MARKET, recorded
    with its kind (`"payload"` vs `"request"` -- they mean opposite
    things), and surfaced in `CollectResult.failure_rate` and
    `CollectResult.summary()`, which the CLI writes into the report and
    checks against `MAX_COLLECT_FAILURE_RATE` before reporting a run as
    clean.

    Args:
        adapter: A `KalshiAdapter` (live, or `httpx.MockTransport`-backed
            in tests).
        markets: Settled markets from `settled_universe`.
        interval_minutes: Candle granularity; one of T1's
            `VALID_INTERVAL_MINUTES`.
        lookback_days: Days of history before each market's close.
        concurrency: Simultaneous candle fetches. See
            `DEFAULT_CONCURRENCY` for the measured 429 headroom.

    Returns:
        CollectResult: Collected markets plus every count needed to say
            what did not survive collection and why.
    """
    semaphore = asyncio.Semaphore(concurrency)
    collected: list[MarketCandles] = []
    failures: list[CollectFailure] = []
    n_short = 0
    for start in range(0, len(markets), COLLECT_BATCH):
        batch = markets[start : start + COLLECT_BATCH]
        done = await asyncio.gather(*(
            _collect_one(
                adapter,
                market,
                interval_minutes=interval_minutes,
                lookback_days=lookback_days,
                semaphore=semaphore,
            )
            for market in batch
        ))
        for outcome in done:
            if outcome is None:
                n_short += 1
            elif isinstance(outcome, CollectFailure):
                failures.append(outcome)
            else:
                collected.append(outcome)
    return CollectResult(
        markets=collected,
        failures=failures,
        n_requested=len(markets),
        n_short_history=n_short,
        n_payload_errors=sum(1 for f in failures if f.kind == "payload"),
        n_request_errors=sum(1 for f in failures if f.kind == "request"),
        interval_minutes=interval_minutes,
        lookback_days=lookback_days,
    )


async def _collect_and_flush(
    adapter: KalshiAdapter,
    markets: Sequence[VenueMarket],
    *,
    interval_minutes: int,
    lookback_days: int,
    concurrency: int,
    cache_path: str | Path | None,
    n_requested: int,
    seed: CollectResult | None = None,
    flush_every: int = CACHE_FLUSH_EVERY,
) -> CollectResult:
    """Collect `markets` via `collect()`, flushing `cache_path` as it goes.

    THE OTHER HALF OF `--cache` (Change 2, 2026-09-07). `write_cache` used
    to run once, after collection finished, so a run that crashed partway
    -- as three live runs did, at samples 8,000/20,000/19,000 -- left no
    cache at all and every already-collected market was thrown away. This
    processes `markets` in `flush_every`-sized slices, writes `cache_path`
    (atomically -- see `write_cache`) after every slice that finishes, and
    once more, with whatever has been collected so far, if a slice raises
    -- before the exception is re-raised. Change 1 (`_collect_one`'s wider
    except tuple) means an ordinary network hiccup no longer reaches this
    function as a raise at all; the flush-on-raise here is the second
    line of defence for whatever still does.

    `seed`, when given, is a previous -- possibly partial -- `CollectResult`
    (typically `read_cache(cache_path)`, read by the caller before
    resampling) whose markets and failures are carried forward and merged
    with whatever this call collects, so a resumed run's cache states
    cumulative progress rather than only this call's slice. `markets`
    must already exclude whatever `seed` accounts for by `market_id` --
    this function does not deduplicate. `n_requested` is the caller's own
    count of the true target (e.g. `len(chosen)`), not derived from
    `seed`/`markets` here, because a market `seed` already counted as
    `n_short_history` cannot be named individually (the cache format
    never recorded which ones they were) and so is retried rather than
    skipped on a resume -- wasted work, never lost or duplicated data,
    but it means `len(seed's markets/failures) + len(markets)` is not
    reliably `n_requested` and must not be used to compute it.

    Returns:
        CollectResult: `seed`'s markets/failures/short-count (if any)
            plus everything newly collected, stamped with `n_requested`.
    """
    all_markets: list[MarketCandles] = list(seed.markets) if seed else []
    all_failures: list[CollectFailure] = list(seed.failures) if seed else []
    n_short = seed.n_short_history if seed else 0

    def _snapshot() -> CollectResult:
        return CollectResult(
            markets=all_markets,
            failures=all_failures,
            n_requested=n_requested,
            n_short_history=n_short,
            n_payload_errors=sum(1 for f in all_failures if f.kind == "payload"),
            n_request_errors=sum(1 for f in all_failures if f.kind == "request"),
            interval_minutes=interval_minutes,
            lookback_days=lookback_days,
        )

    for start in range(0, len(markets), flush_every):
        chunk = markets[start : start + flush_every]
        try:
            chunk_result = await collect(
                adapter,
                chunk,
                interval_minutes=interval_minutes,
                lookback_days=lookback_days,
                concurrency=concurrency,
            )
        except BaseException:
            if cache_path is not None:
                write_cache(cache_path, _snapshot())
            raise
        all_markets.extend(chunk_result.markets)
        all_failures.extend(chunk_result.failures)
        n_short += chunk_result.n_short_history
        if cache_path is not None:
            write_cache(cache_path, _snapshot())
    return _snapshot()


def write_cache(path: str | Path, collected: CollectResult) -> None:
    """Write `collected` to `path` as JSON (the `--cache` contract).

    Candles are stored as positional lists rather than objects: a full
    Gate 1 run is ~8,000 markets x ~240 hourly candles, and the field
    names would be ~80% of the file (PLAN.md D10 keeps these caches out
    of version control for the same reason). The failure counts are
    stored too, so a cached run reports the same survivorship as the
    run that produced it.

    WRITES ATOMICALLY (Change 2, 2026-09-07). This is called repeatedly
    during a single collection run, not just once at the end -- see
    `_collect_and_flush` -- so a crash or Ctrl-C DURING a write is a real
    possibility, not a theoretical one. The payload is serialized to a
    temp file in `path`'s own directory (same filesystem, so the rename
    below is guaranteed atomic rather than a copy) and `os.replace`d onto
    `path`. `os.replace` is atomic on POSIX: a reader (including a later
    run's `read_cache`) either sees the old complete file or the new
    complete file, never a partial one, and a crash mid-write leaves only
    an orphaned temp file, never a truncated `path`.
    """
    payload = {
        "interval_minutes": collected.interval_minutes,
        "lookback_days": collected.lookback_days,
        "n_requested": collected.n_requested,
        "n_short_history": collected.n_short_history,
        "n_payload_errors": collected.n_payload_errors,
        "n_request_errors": collected.n_request_errors,
        "n_universe": collected.n_universe,
        "excluded_by_result": collected.excluded_by_result,
        "failures": [
            {"market_id": f.market_id, "kind": f.kind, "detail": f.detail}
            for f in collected.failures
        ],
        "markets": [
            {
                "venue": m.venue,
                "market_id": m.market_id,
                "event": m.event,
                "series": m.series,
                "close_ts": m.close_ts,
                "result": m.result,
                "outcome": m.outcome,
                "fee": (
                    None
                    if m.fee is None
                    else {
                        "taker_rate": m.fee.taker_rate,
                        "maker_rate": m.fee.maker_rate,
                        "source": m.fee.source,
                        "maker_rebate_rate": m.fee.maker_rebate_rate,
                    }
                ),
                "candles": [
                    [
                        c.end_ts, c.bid_close, c.ask_close, c.px_low,
                        c.px_high, c.px_close, c.volume, c.open_interest,
                    ]
                    for c in m.candles
                ],
            }
            for m in collected.markets
        ],
    }
    target = Path(path)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, allow_nan=False)
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_name)
        raise


def _load_excluded_market_ids(path: str | Path) -> frozenset[str]:
    """Every `market_id` a previous `--cache` run collected (`--exclude-markets-from`).

    T18: a challenger tuned by the two-split rule on `.cache/mm/
    kalshi-60m.json` carries `temporal_win: true` partly BECAUSE it won
    on those 15,283 markets, so scoring it again on any of them is
    in-sample no matter how the parameters were chosen. The only clean
    test draws a fresh sample from markets that cache never touched.

    Reads ONLY the `market_id` field of each cached market, not the
    candles -- `read_cache` would otherwise parse every one into a
    `Candle` tuple for nothing, at real cost on the multi-megabyte cache
    this flag exists to exclude. Identity is all the exclusion contract
    needs.

    Raises:
        FileNotFoundError: If `path` does not exist. There is no
            sensible fallback for "exclude from a file that is not
            there" -- silently excluding nothing would understate the
            overlap this flag exists to guarantee is zero.
    """
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    return frozenset(row["market_id"] for row in payload["markets"])


def _exclude_collected(
    universe: Sequence[VenueMarket], excluded_ids: frozenset[str]
) -> list[VenueMarket]:
    """Drop every market in `excluded_ids` from `universe`, order preserved.

    `--exclude-markets-from`'s whole job (T18): this runs BEFORE
    `random.sample` in `_collect_or_load`, so a market that already
    appears in the excluded cache can never be redrawn into the fresh
    sample -- never sampled and then discarded after the fact, which
    would still have spent a candle fetch on it.
    """
    return [m for m in universe if m.market_id not in excluded_ids]


def _market_event(market: VenueMarket) -> str:
    """The cluster key `_collect_one` will stamp on this market.

    `MarketCandles.event` is `market.event_id or market.market_id`, and
    `MarketCandles.__post_init__` applies the same fallback a second time
    for an empty string. The exclusion contract has to read the SAME
    value from the universe side: keying on a bare `event_id` would let a
    market with no event -- a cluster of one, recorded in the cache under
    its own id -- be redrawn, which is an id-level overlap reintroduced
    by the event filter itself
    (`tests/scripts/test_mm_backtest.py::test_the_event_key_falls_back_to
    _the_market_id_on_both_sides`).
    """
    return market.event_id or market.market_id


def _load_excluded_events(path: str | Path) -> frozenset[str]:
    """Every `event` a previous `--cache` run collected (`--exclude-events-from`).

    WHY AN ID FILTER IS NOT ENOUGH, MEASURED (T19; this flag exists
    because `--exclude-markets-from` shipped and was insufficient). The
    T18 holdout achieved a genuine `market_id` overlap of zero against
    `.cache/mm/kalshi-60m.json` and was still not out of sample: **3,183
    events were shared between the two caches, and 6,073 of the holdout's
    11,911 markets -- 51% -- belonged to a shared event.** Every interval
    this kit reports is clustered BY EVENT (GUARDRAILS.md §2.4) precisely
    because markets inside one event settle on ONE real-world outcome, so
    a test market whose event also fed tuning is not independent of the
    tuning set no matter how distinct its ticker is. Restricted to the
    event-disjoint subset, the same holdout's candidate CI was
    `[-0.1760, +0.9415]` -- a NO-GO where the id-disjoint reading said GO.

    Reads ONLY the `event` field of each cached market, not the candles,
    for the same reason `_load_excluded_market_ids` does: `read_cache`
    would parse every candle into a tuple for nothing, at real cost on
    the multi-hundred-megabyte caches this flag exists to exclude.

    Raises:
        FileNotFoundError: If `path` does not exist. Silently excluding
            nothing would understate exactly the overlap this flag exists
            to drive to zero.
    """
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    return frozenset(row["event"] for row in payload["markets"])


def _exclude_events(
    universe: Sequence[VenueMarket], excluded_events: frozenset[str]
) -> list[VenueMarket]:
    """Drop every market whose EVENT is in `excluded_events`, order preserved.

    Runs BEFORE the sample is drawn in `_collect_or_load`, exactly as
    `_exclude_collected` does, so a market belonging to an already-tuned
    event can never be drawn and then discarded after a candle fetch has
    been spent on it.
    """
    return [m for m in universe if _market_event(m) not in excluded_events]


def _close_week(market: VenueMarket) -> str:
    """The market's ISO year-week of close, in UTC (`"2026-W36"`).

    The stratification bucket. ISO weeks are used rather than 7-day bins
    off an arbitrary epoch so the label is a real calendar object a
    reader can check against the venue, and the zero-padded form sorts
    chronologically as a plain string.
    """
    year, week, _ = market.close_time.astimezone(UTC).isocalendar()
    return f"{year}-W{week:02d}"


def _stratified_sample_by_close_week(
    universe: Sequence[VenueMarket],
    k: int,
    *,
    rng: Any = random,
) -> list[VenueMarket]:
    """Draw `k` markets spread as evenly as possible across close weeks.

    WHY THIS IS NOT `random.sample` (T19, and it is the design problem
    this kit had not confronted). Measured on
    `.cache/mm/kalshi-60m.universe.json` (50,470 settled markets at
    `min_volume=2000`, closes 2026-07-03 -> 2026-09-07): **89% of closes
    fall in the final 7 days of the 65.8-day span.** That skew is itself
    an artifact of the page-capped settled listing
    (`SETTLED_LISTING_PROVENANCE`: 2026-08 is ~94% missing), not a fact
    about when markets settle. Its consequence for a temporal split is
    arithmetic and severe: a 70%-by-market-count train share buys a test
    window of **1.70 days** -- one holiday weekend -- and a 14-day test
    window drawn naturally would contain ~89% of the markets, leaving
    ~11% to train on. Neither is a hold-out anyone should believe.

    So the draw is stratified: each close week gets an equal quota, and a
    week that cannot fill its quota contributes everything it has while
    the shortfall spills onto the weeks that can. Processing weeks in
    ASCENDING order of supply is what makes that one pass rather than an
    iteration -- once a starved week has given all it has, the quota for
    the weeks still to come is recomputed against what is left. The draw
    within each week is uniform and seeded, so `--seed` still reproduces
    the sample exactly.

    THIS COSTS RAW `n` AND IS MEANT TO. The sparse weeks are sparse; an
    even draw is bounded by them, and the achieved distribution is only
    as flat as the venue's visible history allows. That is a finding
    about the listing, not a reason to fall back to a natural draw, and
    it is why the caller prints the achieved per-week counts rather than
    the requested ones.

    Args:
        universe: Markets to draw from, already filtered by volume and by
            whatever exclusions the run applies.
        k: Markets wanted. More than `universe` holds returns the whole
            pool, matching `random.sample(universe, min(k, len(...)))`'s
            contract in `_collect_or_load`.
        rng: Anything with `.sample`; defaults to the module-global
            `random`, which `_main` has already seeded from `--seed`.

    Returns:
        list[VenueMarket]: The drawn markets, in ascending close-week
            order then in draw order within a week.
    """
    buckets: dict[str, list[VenueMarket]] = {}
    for market in universe:
        buckets.setdefault(_close_week(market), []).append(market)

    remaining = min(k, len(universe))
    drawn: dict[str, list[VenueMarket]] = {}
    # Ascending by supply: the weeks that cannot fill a quota are settled
    # first, so their shortfall is still available to redistribute.
    order = sorted(buckets, key=lambda w: (len(buckets[w]), w))
    for position, week in enumerate(order):
        weeks_left = len(order) - position
        quota = remaining // weeks_left
        take = min(len(buckets[week]), quota)
        drawn[week] = rng.sample(buckets[week], take) if take else []
        remaining -= take
    return [m for week in sorted(drawn) for m in drawn[week]]


def read_cache(path: str | Path) -> CollectResult:
    """Read back a `write_cache` file.

    Raises:
        FileNotFoundError: If `path` does not exist -- the CLI treats
            that as "collect instead", exactly as `calibration.py` does.
    """
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    markets = [
        MarketCandles(
            venue=row["venue"],
            market_id=row["market_id"],
            event=row["event"],
            series=row["series"],
            close_ts=int(row["close_ts"]),
            result=row["result"],
            outcome=row.get("outcome", "YES"),
            fee=(
                None
                if row.get("fee") is None
                else FeeSchedule(
                    taker_rate=row["fee"]["taker_rate"],
                    maker_rate=row["fee"]["maker_rate"],
                    source=row["fee"]["source"],
                    maker_rebate_rate=row["fee"].get("maker_rebate_rate", 0.0),
                )
            ),
            candles=tuple(Candle(*values) for values in row["candles"]),
        )
        for row in payload["markets"]
    ]
    return CollectResult(
        markets=markets,
        failures=[
            CollectFailure(f["market_id"], f["kind"], f["detail"])
            for f in payload.get("failures", [])
        ],
        n_requested=int(payload.get("n_requested", len(markets))),
        n_short_history=int(payload.get("n_short_history", 0)),
        n_payload_errors=int(payload.get("n_payload_errors", 0)),
        n_request_errors=int(payload.get("n_request_errors", 0)),
        interval_minutes=int(payload.get("interval_minutes", 0)),
        lookback_days=int(payload.get("lookback_days", 0)),
        n_universe=int(payload.get("n_universe", 0)),
        excluded_by_result=int(payload.get("excluded_by_result", 0)),
    )


# ---------------------------------------------------------------------------
# 3. The replay
# ---------------------------------------------------------------------------


def _mid(candle: Candle) -> float | None:
    """The candle's closing mid, or `None` if it was not two-sided."""
    if candle.bid_close is None or candle.ask_close is None:
        return None
    return (candle.bid_close + candle.ask_close) / 2.0


def _book(market: MarketCandles, candle: Candle) -> OrderBook | None:
    """Build the book `MarketMaker.quote` prices against, or `None`.

    Refuses anything that is not a sane two-sided quote
    (`0 < bid < ask < 1`): a crossed, locked, one-sided or out-of-range
    candle is not a market a resting order could have joined, and
    inventing a mid for one would be inventing the fair value the whole
    policy prices against.
    """
    bid, ask = candle.bid_close, candle.ask_close
    if bid is None or ask is None:
        return None
    if not (0.0 < bid < ask < 1.0):
        return None
    return OrderBook(
        venue=market.venue,
        market_id=market.market_id,
        outcome=market.outcome,
        bids=(BookLevel(bid, _BOOK_LEVEL_SIZE),),
        asks=(BookLevel(ask, _BOOK_LEVEL_SIZE),),
        ts=datetime.fromtimestamp(candle.end_ts, tz=UTC),
    )


def _trades(candle: Candle) -> TradeRange | None:
    """The prints in `candle`, or `None` if nothing printed.

    A zero-volume candle carries no `price` object at all (T1's module
    docstring), so `px_low`/`px_high` are `None` -- no resting order can
    have filled against it. That is NOT a reason to skip the interval:
    the quote still rested and its collateral was still locked, which is
    why the caller counts the quote-hour either way.
    """
    if candle.volume <= 0.0 or candle.px_low is None or candle.px_high is None:
        return None
    return TradeRange(low=candle.px_low, high=candle.px_high, volume=candle.volume)


def _collateral(pair: QuotePair) -> float:
    """USD tied up by the sides `pair` is resting, in contracts x price.

    A resting BUY ties up what it would pay (`price * size`); a resting
    SELL ties up what a short contract pays out if YES resolves
    (`(1 - price) * size`). Only the sides actually quoted count -- an
    inventory limit that withdrew one side released its collateral.
    """
    total = 0.0
    if pair.bid is not None:
        total += pair.bid.price * pair.bid.size
    if pair.ask is not None:
        total += (1.0 - pair.ask.price) * pair.ask.size
    return total


def _rebate(fill: PassiveFill, fee_model: FeeModel, schedule: FeeSchedule) -> float:
    """What the published maker rebate WOULD pay on `fill`.

    The rebate is a share of the TAKER fee the other side paid
    (`FeeSchedule.maker_rebate_rate`, e.g. 0.25 for 25%). Computed here
    so the report can state it as its own line, and computed OUTSIDE
    `FeeModel.fee()` because `fee()` never credits it (GUARDRAILS.md
    §2.3, PLAN.md D6): a rebate is a programme payout under terms the
    venue can change, and crediting it inside the fee model would let
    projected revenue leak into every cost figure in this repo.
    """
    if schedule.maker_rebate_rate <= 0.0:
        return 0.0
    taker_fee = fee_model.fee(fill.price, fill.size, "taker", schedule)
    return schedule.maker_rebate_rate * taker_fee


def replay(
    markets_with_candles: Sequence[MarketCandles],
    *,
    policy: MarketMaker,
    fill_model: FillModel,
    tick_size: float = 0.01,
    fee_model: FeeModel | None = None,
    schedule: FeeSchedule | None = None,
) -> ReplayResult:
    """Replay `policy` over `markets_with_candles` under one fill model.

    See the module docstring for the quote/fill/mark triple and for the
    two P&L numbers every row carries: `pnl`, the cash-settled money the
    verdict is computed on, and `markout_pnl`, the mark-dependent
    quote-quality statistic reported beside it.

    Args:
        markets_with_candles: Settled markets with their history.
        policy: The quoting policy. Stateless with respect to the venue
            -- inventory is passed back in, so the same object replays
            here and drives a paper run identically.
        fill_model: `"optimistic"` (a print AT your price fills you) or
            `"pessimistic"` (only a print THROUGH it does). The two
            differ in SIGN on the same data; a result quoting one
            without naming it is not a result (GUARDRAILS.md §2.1).
        tick_size: The market's minimum price increment.
        fee_model: Venue fee model; defaults to `KalshiFeeModel`. T11
            passes `PolymarketFeeModel` so both venues run through this
            same function (PLAN.md D11).
        schedule: Fallback fee schedule for markets carrying none of
            their own; defaults to `default_kalshi_schedule()`. A
            market's own `MarketCandles.fee` wins over it (PLAN.md D9:
            schedules change per market over time).

    Returns:
        ReplayResult: One `MarketRow` per input market, in input order.
    """
    model = fee_model if fee_model is not None else KalshiFeeModel()
    fallback = schedule if schedule is not None else default_kalshi_schedule()
    rows: list[MarketRow] = []
    n_intervals = 0
    n_unmarkable = 0

    for market in markets_with_candles:
        market_schedule = market.fee if market.fee is not None else fallback
        engine = PassiveFillEngine(model, market_schedule, fill_model=fill_model)
        candles = market.candles
        inventory = 0.0
        cash = 0.0
        markout = 0.0
        rebate = 0.0
        last_mid: float | None = None
        quote_hours = 0
        n_fills = 0
        n_two_sided = 0
        collateral_total = 0.0

        for i in range(len(candles) - 2):
            n_intervals += 1
            book = _book(market, candles[i])
            if book is None:
                continue
            # T6: how long until this market closes, as of the candle the
            # quote is drawn from. `MarketMaker.taper_hours` is 0.0 by
            # default, which makes this inert for every existing caller
            # (`_effective_max_inventory` short-circuits on it) -- passed
            # unconditionally rather than only when a taper is configured
            # so a caller that DOES set `taper_hours` needs no separate
            # code path through this loop.
            hours_to_close = (market.close_ts - candles[i].end_ts) / 3600.0
            pair = policy.quote(
                book, tick_size=tick_size, inventory=inventory,
                hours_to_close=hours_to_close,
            )
            if not pair.quotes:
                continue
            # The quote rested: its collateral was locked whether or not
            # anything traded against it, and whether or not the mark
            # for it turns out to be readable.
            quote_hours += 1
            collateral_total += _collateral(pair)
            if pair.is_two_sided:
                n_two_sided += 1

            mark = _mid(candles[i + 2])
            if mark is None:
                # No two-sided book one interval after the fill interval
                # means no fair value to score against. Counted rather
                # than scored at a substitute -- the mark is the whole
                # no-look-ahead property.
                n_unmarkable += 1
                continue
            trades = _trades(candles[i + 1])
            if trades is None:
                continue
            for fill in engine.fills(pair, trades):
                # The money: a buy pays out `price * size`, a sell takes
                # it in, and the maker fee is a cost either way. No mark
                # is consulted -- see "TWO P&L NUMBERS" in the module
                # docstring for why that independence is structural here
                # rather than a cancellation to be trusted.
                direction = 1.0 if fill.side == "buy" else -1.0
                cash += -direction * fill.price * fill.size - fill.fee
                # The quality statistic, marked one full interval after
                # the fill interval.
                markout += mark_to_market(fill, mark)
                rebate += _rebate(fill, model, market_schedule)
                inventory += direction * fill.size
                n_fills += 1
                last_mid = mark

        # Terminal inventory settles at the venue's real result, never
        # at a mark (PLAN.md D4). The money receives the whole
        # settlement value; the markout statistic receives only the move
        # from `last_mid`, since it already charged everything up to
        # there against the quotes.
        held = inventory != 0.0
        if held:
            cash += inventory * market.settle
            if last_mid is not None:
                markout += inventory * (market.settle - last_mid)

        rows.append(MarketRow(
            market_id=market.market_id,
            event=market.event,
            series=market.series,
            close_ts=market.close_ts,
            quote_hours=quote_hours,
            n_fills=n_fills,
            n_two_sided=n_two_sided,
            pnl=cash,
            markout_pnl=markout,
            collateral_mean=(
                collateral_total / quote_hours if quote_hours else 0.0
            ),
            terminal_inventory=inventory,
            held_into_settlement=held,
            settled_short_into_yes=inventory < 0.0 and market.result == "yes",
            rebate_if_paid=rebate,
            last_mid=last_mid,
        ))

    return ReplayResult(
        fill_model=fill_model,
        rows=tuple(rows),
        n_intervals=n_intervals,
        n_unmarkable=n_unmarkable,
    )


# ---------------------------------------------------------------------------
# 4. The report
# ---------------------------------------------------------------------------


def _finite(value: float | None) -> float | None:
    """`value` if it is a finite float, else `None`.

    Every number leaving this module goes through here so the JSON is
    STRICT JSON: `json.dump`'s default `allow_nan=True` emits the
    non-standard `NaN`/`Infinity` tokens, which most readers of a report
    file will not accept, and an unavailable interval must say so as
    `null` rather than as a token that looks like a number.
    """
    if value is None or not math.isfinite(value):
        return None
    return float(value)


def _power_table(
    observations: Sequence[tuple[str, float]],
    *,
    fill_model: FillModel,
    seed: int,
) -> dict[str, Any]:
    """Portfolio-size power, by resampling whole EVENTS of cash P&L.

    Answers the only question a per-market mean cannot: how many
    simultaneous markets it takes before the strategy's own variance
    stops being the dominant term (the preceding session put that at
    ~2,500).

    WHAT "PORTFOLIO SIZE 500" MEANS HERE. The unit drawn is an EVENT,
    not a market: markets inside one event settle on one real-world
    outcome (the 49 rungs of `KXBTCD-26SEP0417` are one day's BTC print,
    not 49 bets), so drawing them separately would count one outcome as
    many independent ones. A row labelled `portfolio_markets: 500` is
    therefore *500 markets' worth of whole events* -- `round(500 /
    mean_markets_per_event)` events drawn WITH REPLACEMENT, reported as
    `portfolio_events`, each contributing its whole summed P&L. The
    realized market count is random with mean 500 rather than exactly
    500, and `mean_markets_per_event` is on the block so a reader can
    recover it.

    THIS IS NOT A COSMETIC CHANGE TO THE STATISTIC. On a pool of 40
    events of 25 same-signed markets, an i.i.d. market draw gives a 500-
    market portfolio sd of sqrt(500) = 22.4 while the event draw gives
    25*sqrt(20) = 111.8 -- a portfolio five times safer than the data
    supports, purely from treating 25 copies of one outcome as 25
    independent bets (`tests/scripts/test_mm_backtest.py::test_the_
    resample_draws_whole_events_not_single_markets`). The previous
    version drew markets i.i.d. and merely SAID SO in `resample`;
    labelling an assumption is not the same as its being true, and
    nothing in the sample made it true.

    EVERY BLOCK STATES ITS OWN POOL, AND REFUSES BELOW A FLOOR. The
    printed table renders these blocks away from the `n_trading` they
    were drawn from, so `pool_n_markets`, `pool_n_events` and
    `pool_floor_events` travel with each one -- a reader of a single row
    can otherwise not tell three markets from three thousand, nor forty
    markets in forty events from forty in one. Below
    `MIN_POWER_POOL_EVENTS` (see that constant for the reasoning and for
    the live concentration this fixes) both figures come back `None`
    with `insufficient_sample` true, mirroring the convention `report()`
    already uses for the CI: an unavailable number says so rather than
    printing a number the sample cannot support. Because the floor
    counts events and exceeds `CI_FLOOR_EVENTS`, this table can never
    speak where `ci95_clustered_by_event` refuses.

    Args:
        observations: `(event, pnl)` per TRADING market in scope, where
            `pnl` is the cash-settled money -- not `markout_pnl`, power
            is about what was banked. The event key is required: it is
            the unit of independence the whole statistic rests on.
        fill_model: The queue assumption every figure was produced
            under.
        seed: Seeds the resample, so the table is reproducible.

    Returns:
        dict[str, Any]: One labelled block per entry of
            `PORTFOLIO_SIZES`, keyed by the size as a string.
    """
    rng = random.Random(seed)

    pools: dict[str, list[float]] = {}
    for event, pnl in observations:
        pools.setdefault(event, []).append(pnl)
    event_pnl = [math.fsum(members) for members in pools.values()]
    n_events = len(event_pnl)
    n_markets = len(observations)

    # THE EMPTY-POOL GUARD, kept deliberately separate from the evidence
    # floor below. `rng.choices` raises IndexError on an empty
    # population, and for one release the ONLY thing preventing that was
    # `MIN_POWER_POOL_MARKETS` happening to be greater than zero:
    # deleting the floor for statistical reasons produced eleven test
    # failures, seven of them unrelated tests dying with IndexError. One
    # constant serving both a statistical threshold and a crash guard
    # means anyone retuning the evidence floor silently retunes crash
    # behaviour. This check owes nothing to the floor's value and holds
    # with the floor set to zero.
    empty_pool = n_events == 0
    insufficient = empty_pool or n_events < MIN_POWER_POOL_EVENTS
    mean_markets_per_event = None if empty_pool else n_markets / n_events

    indices = range(n_events)
    table: dict[str, Any] = {}
    for size in PORTFOLIO_SIZES:
        block: dict[str, Any] = {
            "fill_model": fill_model,
            "terminal": TERMINAL,
            "portfolio_markets": size,
            "portfolio_events": None,
            "resample": "whole_events_with_replacement",
            "statistic": "cash_settled_pnl_per_trading_market",
            "pool_n_markets": n_markets,
            "pool_n_events": n_events,
            "pool_floor_events": MIN_POWER_POOL_EVENTS,
            "mean_markets_per_event": _finite(mean_markets_per_event),
            "insufficient_sample": insufficient,
        }
        if insufficient:
            # Not "no data" -- a refusal. A `p_profit` drawn from a pool
            # this thin, or this concentrated, is a restatement of the
            # pool's sign rather than a measurement of a portfolio.
            block["pct5_total_pnl"] = None
            block["p_profit"] = None
        else:
            # `mean_markets_per_event` is not None here: `insufficient`
            # already absorbed the empty pool.
            draws = max(1, round(size / mean_markets_per_event))
            block["portfolio_events"] = draws
            totals = sorted(
                math.fsum(event_pnl[i] for i in rng.choices(indices, k=draws))
                for _ in range(BOOTSTRAP_REPLICATES)
            )
            block["pct5_total_pnl"] = _finite(totals[int(0.05 * len(totals))])
            block["p_profit"] = sum(1 for t in totals if t > 0.0) / len(totals)
        table[str(size)] = block
    return table


def _block(
    rows: Sequence[MarketRow], *, fill_model: FillModel, seed: int
) -> dict[str, Any]:
    """One labelled numeric block for a set of markets.

    `n_quoted` counts MARKETS the policy rested a quote in (not
    quote-hours) and `n_trading` counts markets that actually filled;
    `roc = total_pnl / (n_quoted * collateral_mean)` is therefore return
    on the capital a quoted market ties up, which is the denominator the
    `edge_fraction` calibration in `market_making.py` was measured
    against.

    BOTH P&L NUMBERS APPEAR, and only one of them drives anything. The
    `total_pnl`/`mean_pnl`/`sd_pnl`/`ci95`/`roc` family is the
    cash-settled money (`pnl_basis`); the `*_markout_pnl` family is the
    mark-dependent quote-quality statistic (`markout_basis`), reported
    beside it so a reader can see the carried-inventory drift the money
    contains and the markout does not. The interval, the return on
    capital and the power table are computed on the money -- the
    verdict is a claim about a business, and a mark-dependent number
    would put the choice of candle inside it (module docstring, "WHY
    THIS IS SPLIT IN TWO").
    """
    quoted = [r for r in rows if r.quote_hours > 0]
    trading = [r for r in rows if r.n_fills > 0]
    total_pnl = math.fsum(r.pnl for r in rows)
    total_markout = math.fsum(r.markout_pnl for r in rows)
    collateral_mean = (
        statistics.fmean(r.collateral_mean for r in quoted) if quoted else 0.0
    )
    denominator = len(quoted) * collateral_mean

    # `cluster_bootstrap` (reused from `app.scripts.calibration` rather
    # than reimplemented -- PLAN.md D11's whole point) draws from the
    # module-global `random`, so seeding it here is what makes the
    # interval reproducible from `--seed` alone. It is deliberately
    # seeded per call, so a block's interval does not depend on how many
    # blocks were computed before it.
    random.seed(seed)
    lo, hi = (
        cluster_bootstrap(
            [{"event": r.event, "pnl": r.pnl} for r in trading],
            lambda sample: statistics.fmean(r["pnl"] for r in sample),
            BOOTSTRAP_REPLICATES,
        )
        if trading
        else (math.nan, math.nan)
    )

    pnls = [r.pnl for r in trading]
    markouts = [r.markout_pnl for r in trading]
    return {
        "fill_model": fill_model,
        "terminal": TERMINAL,
        # Which arithmetic each family of figures below is, named on the
        # block so a block lifted out of the report still says what it
        # measured (module docstring, "TWO P&L NUMBERS").
        "pnl_basis": "cash_settled",
        "markout_basis": "marked_at_i_plus_2",
        "n_markets": len(rows),
        "n_quoted": len(quoted),
        "n_trading": len(trading),
        "n_events": len({r.event for r in rows}),
        # The bootstrap clusters over the events of TRADING markets
        # only, and returns no interval below five of them -- so this,
        # not `n_events`, is the number that says whether the interval
        # beside it could be computed at all.
        "n_events_trading": len({r.event for r in trading}),
        "quote_hours": sum(r.quote_hours for r in rows),
        "n_fills": sum(r.n_fills for r in rows),
        "n_two_sided": sum(r.n_two_sided for r in rows),
        "total_pnl": _finite(total_pnl),
        "mean_pnl_per_trading_market": _finite(
            total_pnl / len(trading) if trading else None
        ),
        "sd_pnl_per_trading_market": _finite(
            statistics.stdev(pnls) if len(pnls) > 1 else None
        ),
        "total_markout_pnl": _finite(total_markout),
        "mean_markout_pnl_per_trading_market": _finite(
            total_markout / len(trading) if trading else None
        ),
        "sd_markout_pnl_per_trading_market": _finite(
            statistics.stdev(markouts) if len(markouts) > 1 else None
        ),
        "ci95_clustered_by_event": [_finite(lo), _finite(hi)],
        "collateral_mean": _finite(collateral_mean),
        "roc": _finite(total_pnl / denominator if denominator > 0.0 else None),
        "held_into_settlement": sum(1 for r in rows if r.held_into_settlement),
        "settled_short_into_yes": sum(
            1 for r in rows if r.settled_short_into_yes
        ),
        "rebate_if_paid_not_in_pnl": _finite(
            math.fsum(r.rebate_if_paid for r in rows)
        ),
        # The event key travels WITH the P&L into the power table. It
        # used to be dropped here (`_power_table` received a flat list of
        # floats), which is what let a pool of 40 markets in ONE event
        # clear a floor while the CI two lines above correctly refused
        # it -- the table's independence assumption is event-level, so
        # its input has to be.
        "power": _power_table(
            [(r.event, r.pnl) for r in trading],
            fill_model=fill_model,
            seed=seed,
        ),
    }


@dataclass(frozen=True)
class SplitParts:
    """A (train, test) partition and what it had to drop to stay clean.

    Attributes:
        train: Rows scored as the in-sample half.
        test: Rows scored as the out-of-sample half. Never shares an
            `event` with `train` -- see `dropped_from_test`.
        straddling_events: Events with members on BOTH sides of a
            temporal cutoff. Always 0 for an event split, where whole
            events go to one side by construction.
        dropped_from_test: Post-cutoff markets excluded because their
            event also has a pre-cutoff member. They appear in neither
            half, so `len(train) + len(test)` is short of `len(rows)` by
            exactly this many.
    """

    train: list[MarketRow]
    test: list[MarketRow]
    straddling_events: int
    dropped_from_test: int


def _split(
    rows: Sequence[MarketRow],
    *,
    split: SplitKind,
    cutoff_ts: int | None,
    seed: int,
) -> SplitParts:
    """Partition `rows` into (train, test) per PLAN.md D5.

    THE TEMPORAL SPLIT IS BY TIME AND THEN BY EVENT, IN THAT ORDER.
    GUARDRAILS.md §4.1 and T2's acceptance (g) require the boundary to
    be `close_ts` -- a random split cannot detect regime change and the
    go/no-go must. Partitioning individual ROWS on `close_ts` satisfies
    that and still leaks: an event whose markets close on both sides of
    the cutoff puts one correlated real-world outcome in both halves, so
    a test-split market shares its answer with a train-split market,
    which is the specific thing an out-of-sample test exists to prevent.
    Measured live on the real universe, 2026-09-07: **16 of 18,457
    events (128 of 50,796 markets) straddle the median close** -- 0.25%,
    small enough that it was invisible and large enough that the verdict
    is computed with it.

    So the temporal semantics are kept and the straddlers are removed
    from the TEST side only: their pre-cutoff markets stay in `train`
    (dropping those would discard evidence the test half never sees
    anyway) and their post-cutoff markets are dropped entirely. Removing
    them from `train` instead would not work -- the leak runs both ways
    and only the half the verdict is computed on has to be clean. The
    counts come back on `SplitParts` and reach the JSON and the printed
    header either way, because a rule that silently stopped running must
    not look like a clean split.

    Raises:
        ValueError: If `split` is `"temporal"` without a `cutoff_ts`
            (there is no defensible default for the boundary of a
            go/no-go), or if `split` is not a `SplitKind`.
    """
    if split == "temporal":
        if cutoff_ts is None:
            raise ValueError("a temporal split needs a cutoff_ts")
        train = [r for r in rows if r.close_ts < cutoff_ts]
        straddling = {r.event for r in train} & {
            r.event for r in rows if r.close_ts >= cutoff_ts
        }
        test = [
            r
            for r in rows
            if r.close_ts >= cutoff_ts and r.event not in straddling
        ]
        return SplitParts(
            train=train,
            test=test,
            straddling_events=len(straddling),
            dropped_from_test=(
                sum(1 for r in rows if r.close_ts >= cutoff_ts) - len(test)
            ),
        )
    if split != "event":
        raise ValueError(f"split must be 'event' or 'temporal', got {split!r}")
    # Whole EVENTS go to one side or the other: markets inside one event
    # share an outcome, so splitting markets independently would leak
    # the answer across the boundary. Nothing can straddle by
    # construction, which is why the counts below are structurally zero
    # rather than merely unmeasured.
    events = sorted({r.event for r in rows})
    random.Random(seed).shuffle(events)
    train_events = set(events[: len(events) // 2])
    return SplitParts(
        train=[r for r in rows if r.event in train_events],
        test=[r for r in rows if r.event not in train_events],
        straddling_events=0,
        dropped_from_test=0,
    )


def report(
    result: ReplayResult,
    *,
    split: SplitKind,
    cutoff_ts: int | None = None,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Score one `ReplayResult` into the report JSON's per-model block.

    Called once per fill model by the CLI (and by T4/T5/T6/T11), so the
    two models are always reported side by side and never one alone
    (PLAN.md D3).

    THE VERDICT EXISTS ONLY ON A TEMPORAL SPLIT (PLAN.md D5,
    GUARDRAILS.md §4.1). An event split is for parameter tuning: scoring
    the go/no-go on the same split a parameter was chosen on is not out
    of sample, and a report that says it is would be lying. So
    `split="event"` returns `verdict: None` -- deliberately present and
    null rather than absent, so a reader who looks for it finds an
    answer.

    Args:
        result: One fill model's replay.
        split: `"temporal"` for the go/no-go, `"event"` for tuning.
        cutoff_ts: Unix timestamp splitting train (`close_ts <`) from
            test (`close_ts >=`). Required for `split="temporal"`.
        seed: Seeds both the cluster bootstrap and the power table, so
            the report is reproducible.

    WHAT `overall` IS AND IS NOT. It scores EVERY row, so it is the
    whole sample rather than the union of the two halves: a temporal
    split drops the post-cutoff markets of any event that straddles the
    cutoff (`_split`), and those rows are still in `overall`.
    `split_exclusions` carries the counts so the difference is readable
    rather than something a reader has to notice by subtracting.

    Returns:
        dict[str, Any]: `fill_model` and `terminal` at the top level and
            on every nested numeric block (GUARDRAILS.md §2.1), the
            `overall`/`train`/`test` blocks, `split_exclusions`, and the
            verdict.

    Raises:
        ValueError: Per `_split`.
    """
    parts = _split(result.rows, split=split, cutoff_ts=cutoff_ts, seed=seed)
    fill_model = result.fill_model
    overall = _block(result.rows, fill_model=fill_model, seed=seed)
    test_block = _block(parts.test, fill_model=fill_model, seed=seed)

    verdict: dict[str, Any] | None = None
    if split == "temporal":
        lower = test_block["ci95_clustered_by_event"][0]
        verdict = {
            "fill_model": fill_model,
            "terminal": TERMINAL,
            "basis": "temporal_test",
            # The go/no-go is a claim about money, so it is computed on
            # the cash-settled figure and never on `markout_pnl`, which
            # would put the choice of mark candle inside the verdict.
            "pnl_basis": "cash_settled",
            "statistic": "mean_pnl_per_trading_market",
            "ci95_lower": lower,
            "lower_bound_above_zero": (None if lower is None else lower > 0.0),
            "n_trading": test_block["n_trading"],
            "n_events_trading": test_block["n_events_trading"],
        }

    return {
        "fill_model": fill_model,
        "terminal": TERMINAL,
        "split": split,
        "cutoff_ts": cutoff_ts,
        "seed": seed,
        "n_intervals": result.n_intervals,
        "n_unmarkable_intervals": result.n_unmarkable,
        "overall": overall,
        "train": _block(parts.train, fill_model=fill_model, seed=seed),
        "test": test_block,
        # Counts, not P&L, but labelled anyway: the printed table and
        # the JSON both render this block away from the header that
        # names the model, and §2.1's habit is cheaper to keep than to
        # decide about per block.
        "split_exclusions": {
            "fill_model": fill_model,
            "terminal": TERMINAL,
            "rule": "straddling_events_excluded_from_test",
            "straddling_events": parts.straddling_events,
            "markets_dropped_from_test": parts.dropped_from_test,
            "detail": (
                "events with markets closing on both sides of the cutoff "
                "keep their pre-cutoff markets in train and have their "
                "post-cutoff markets dropped, so no test market shares an "
                "event with a train market. Those rows remain in `overall`."
            ),
        },
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# 5. The printed table
# ---------------------------------------------------------------------------


def _fmt(value: float | None, spec: str = "+10.4f") -> str:
    """One table cell: the number, or a dash filling the same column.

    The dash's width is read off `spec` rather than hard-coded, so an
    unavailable figure occupies exactly the column it would have filled.
    A fixed-width dash silently breaks alignment the moment a column
    changes width -- which is how the refused power table first rendered
    as `- -`.
    """
    if value is None:
        digits = "".join(ch for ch in spec.split(".")[0] if ch.isdigit())
        return "-".rjust(int(digits) if digits else 10)
    return format(value, spec)


def _print_model(payload: dict[str, Any]) -> None:
    """Print one fill model's blocks, `overall` / `train` / `test`."""
    print(
        f"\n--- fill_model={payload['fill_model']} terminal={payload['terminal']}"
        f" split={payload['split']} ---"
    )
    header = (
        f"{'block':>8} {'n_quoted':>9} {'n_trading':>10} {'mean cash P&L':>14}"
        f" {'95% CI (event-clustered)':>26} {'mean markout':>13} {'ROC':>9}"
        f" {'held':>6} {'short->yes':>11}"
    )
    print(header)
    for name in ("overall", "train", "test"):
        block = payload[name]
        lo, hi = block["ci95_clustered_by_event"]
        interval = (
            "                         -"
            if lo is None or hi is None
            else f"[{lo:+11.4f},{hi:+11.4f}]"
        )
        print(
            f"{name:>8} {block['n_quoted']:9d} {block['n_trading']:10d}"
            f" {_fmt(block['mean_pnl_per_trading_market'], '+14.4f')}"
            f" {interval:>26}"
            f" {_fmt(block['mean_markout_pnl_per_trading_market'], '+13.4f')}"
            f" {_fmt(block['roc'], '+9.4f')} {block['held_into_settlement']:6d}"
            f" {block['settled_short_into_yes']:11d}"
        )
    print(
        "  cash P&L is the money (fills settled at the venue result, no"
        " mark); markout marks each fill at i+2 and is a quote-quality"
    )
    print(
        "  statistic only -- the CI, the ROC and the verdict are computed"
        " on the cash figure."
    )
    rebate = payload["overall"]["rebate_if_paid_not_in_pnl"]
    print(
        f"  maker rebate would add ${rebate:.4f} if paid as published"
        " -- NOT included in any P&L above (GUARDRAILS.md 2.3)"
    )
    exclusions = payload["split_exclusions"]
    if payload["split"] == "temporal":
        # Printed whether or not anything was dropped: a clean split and
        # a rule that silently stopped running must not look alike.
        print(
            f"  temporal split: {exclusions['straddling_events']} event(s)"
            " straddle the cutoff;"
            f" {exclusions['markets_dropped_from_test']} post-cutoff"
            " market(s) DROPPED from test so no test market shares an event"
            " with a train market (they remain in overall)"
        )
    scope = "test" if payload["split"] == "temporal" else "overall"
    pool = payload[scope]["power"][str(PORTFOLIO_SIZES[0])]
    print(
        f"  power ({scope}, resampling WHOLE EVENTS with replacement from"
        f" the trading markets' cash P&L; pool={pool['pool_n_markets']}"
        f" markets in {pool['pool_n_events']} events,"
        f" floor={pool['pool_floor_events']} events):"
    )
    print(
        f"      {'portfolio':>10} {'events drawn':>13} {'5th pct total':>15}"
        f" {'P(profit)':>10}"
    )
    for size in PORTFOLIO_SIZES:
        cell = payload[scope]["power"][str(size)]
        p_profit = cell["p_profit"]
        events = cell["portfolio_events"]
        print(
            f"      {size:10d}"
            f" {'-'.rjust(13) if events is None else format(events, '13d')}"
            f" {_fmt(cell['pct5_total_pnl'], '+15.4f')}"
            f" {'-'.rjust(10) if p_profit is None else format(p_profit, '10.4f')}"
        )
    if pool["insufficient_sample"]:
        # A resample from a pool this thin -- or this CONCENTRATED --
        # restates the pool's sign at every portfolio size; printing
        # 1.0000 beneath a verdict that says its own CI is unavailable is
        # the defect this line replaces.
        print(
            f"      insufficient sample: {pool['pool_n_events']} trading"
            f" event(s) ({pool['pool_n_markets']} market(s)) is below the"
            f" floor of {pool['pool_floor_events']} events; no P(profit) or"
            " 5th percentile is reported"
        )
    verdict = payload["verdict"]
    if verdict is not None:
        above = verdict["lower_bound_above_zero"]
        state = "unavailable" if above is None else ("ABOVE 0" if above else "not above 0")
        print(
            f"  VERDICT (fill_model={verdict['fill_model']},"
            f" terminal={verdict['terminal']}, basis={verdict['basis']}):"
            f" lower CI bound {_fmt(verdict['ci95_lower'])} is {state}"
            f" (n_trading={verdict['n_trading']},"
            f" n_events_trading={verdict['n_events_trading']})"
        )


def print_report(payload: dict[str, Any]) -> None:
    """Print the whole report: data window first, then pessimistic, then
    optimistic (GUARDRAILS.md §4.5 and §2.2)."""
    window = payload["data_window"]
    collection = payload["collection"]
    policy = payload["policy"]
    print("=" * 78)
    print("mm_backtest -- passive quoting replayed over settled history")
    print("=" * 78)
    print(
        f"data window: venue={window['venue']}"
        f" interval={window['interval_minutes']}m"
        f" lookback={window['lookback_days']}d"
    )
    print(
        f"             closes {window['first_close']} -> {window['last_close']}"
    )
    print(
        f"             markets replayed {window['n_markets']}"
        f"  candles {window['n_candles']}"
    )
    for model in ("pessimistic", "optimistic"):
        block = payload[model]["overall"]
        print(
            f"             n_quoted {block['n_quoted']}"
            f"  n_trading {block['n_trading']}  [fill_model={model},"
            f" terminal={TERMINAL}]"
        )
    print(
        f"universe:    settled with volume >= {collection['min_volume']:.0f}:"
        f" {collection['n_universe']}"
        f"   excluded by result: {collection['excluded_by_result']}"
        f" ({collection['survivorship_share']:.4f} of settled)"
    )
    # GUARDRAILS.md §7: `n_universe` above is a count of a TRUNCATED
    # listing, and survivorship_share beside it measures something else
    # entirely. The caveat prints with the number rather than living in
    # NOTES.md, because the reader who copies the number is the reader
    # who has to see it (see SETTLED_LISTING_PROVENANCE).
    provenance = collection["universe_provenance"]
    tradeable = (
        provenance["visible_tradeable_markets"]
        + provenance["invisible_tradeable_markets"]
    )
    print(
        "             CAVEAT -- PAGE-CAPPED LISTING: n_universe counts a"
        f" listing truncated at {provenance['cap'].split(';')[0].strip()},"
        " not the venue's settled universe."
    )
    print(
        f"             measured {provenance['measured_on']}:"
        f" {provenance['visible_tradeable_markets']:,} of {tradeable:,}"
        " tradeable settled markets visible"
        f" ({provenance['visible_share_of_tradeable_universe']:.2f});"
        " 2026-08 is 94% missing and the bias is month-correlated."
    )
    print(
        "             So this sample is NOT a random draw from the venue,"
        " and that is a different fact from survivorship_share above."
    )
    print(
        f"collection:  requested {collection['n_requested']}"
        f"  collected {collection['n_collected']}"
        f"  short history {collection['n_short_history']}"
        f"  payload errors {collection['n_payload_errors']}"
        f"  request errors {collection['n_request_errors']}"
    )
    print(
        f"policy:      min_spread={policy['min_spread']}"
        f" edge_fraction={policy['edge_fraction']}"
        f" max_inventory={policy['max_inventory']}"
        f" skew_strength={policy['skew_strength']}"
        f" quote_size={policy['quote_size']}"
    )
    print(
        f"fees:        {policy['fee_model']}"
        f" maker_rate={policy['maker_rate']}"
        f" taker_rate={policy['taker_rate']}"
        f" source={policy['fee_source']}"
    )
    for model in ("pessimistic", "optimistic"):
        _print_model(payload[model])


# ---------------------------------------------------------------------------
# 6. CLI
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay MarketMaker over settled Kalshi history."
    )
    parser.add_argument("--sample", type=int, default=500,
                        help="settled markets to replay")
    parser.add_argument("--min-volume", type=float, default=DEFAULT_MIN_VOLUME,
                        help="volume floor for the settled universe")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_MINUTES,
                        help="candle granularity in minutes (1, 60 or 1440)")
    parser.add_argument("--days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                        help="days of history before each market's close")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help="simultaneous candle fetches")
    parser.add_argument("--cache", type=str, default=None,
                        help="read/write collected candles here as JSON; also "
                             "controls the settled-universe cache beside it "
                             "(see --refresh-universe)")
    parser.add_argument("--refresh-universe", action="store_true",
                        help="re-walk the settled universe even if a cached "
                             "one exists at the same --min-volume, instead of "
                             "loading it")
    parser.add_argument("--exclude-markets-from", type=str, default=None,
                        help="a previous --cache file; every market_id it "
                             "holds is removed from the universe BEFORE "
                             "sampling (T18: score a candidate on markets "
                             "that never touched its selection)")
    parser.add_argument("--exclude-events-from", type=str, default=None,
                        help="a previous --cache file; every market whose "
                             "EVENT it holds is removed from the universe "
                             "BEFORE sampling. Stronger than "
                             "--exclude-markets-from and the one that "
                             "matters: the CI is clustered by event, so the "
                             "event is the unit of independence (T19)")
    parser.add_argument("--stratify-by-close-week", action="store_true",
                        help="draw --sample evenly across ISO close weeks "
                             "instead of proportionally, so a multi-week "
                             "temporal test window exists at all (T19: 89% "
                             "of visible closes fall in the final 7 days)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--temporal-cutoff", type=str, default=None,
                        help="YYYY-MM-DD; train is closes BEFORE it, test at "
                             "or after. Defaults to the median close.")
    parser.add_argument("--out", type=str, default=None,
                        help="write the report JSON here")
    parser.add_argument("--min-spread", type=float, default=DEFAULT_MIN_SPREAD)
    parser.add_argument("--edge-fraction", type=float,
                        default=DEFAULT_EDGE_FRACTION)
    parser.add_argument("--max-inventory", type=float,
                        default=DEFAULT_MAX_INVENTORY)
    parser.add_argument("--skew-strength", type=float,
                        default=DEFAULT_SKEW_STRENGTH)
    parser.add_argument("--quote-size", type=float, default=DEFAULT_QUOTE_SIZE)
    return parser


def _cutoff_ts(markets: Sequence[MarketCandles], raw: str | None) -> tuple[int, str]:
    """Resolve `--temporal-cutoff`, defaulting to the median close.

    A fixed calendar date is the right cutoff for the Gate 1 report,
    where the window is known; the median close is the right DEFAULT,
    because a small `--sample` over a short `--days` can otherwise land
    every market on one side of an arbitrary date and produce a
    degenerate hold-out that looks like a result. The choice is recorded
    in the JSON as `cutoff_source` either way.
    """
    if raw is not None:
        parsed = datetime.fromisoformat(raw).replace(tzinfo=UTC)
        return int(parsed.timestamp()), "argument"
    closes = sorted(m.close_ts for m in markets)
    if not closes:
        return 0, "empty"
    return closes[len(closes) // 2], "median_close"


async def _collect_or_load(args: argparse.Namespace) -> CollectResult:
    """Collect candles, or resume/read back a previous run's `--cache`.

    Three cases, in order:

    1. No `--cache`, or the file does not exist yet: collect everything
       from scratch (case 3's machinery still runs, it just starts from
       an empty `seed`).
    2. `--cache` points at a run that FINISHED -- every market it was
       asked for is accounted for as collected, failed, or too short
       (`accounted >= cached.n_requested`, checked by aggregate count
       since short-history markets are not individually named in the
       cache). Returned as-is: the whole point of `--cache` is that a
       completed run is never re-collected, and this is the only branch
       that never touches `settled_universe` or the network.
    3. `--cache` points at a run an earlier crash INTERRUPTED (Change 2,
       2026-09-07) -- fewer markets are accounted for than were
       requested. The outstanding markets (by `market_id`, against the
       same `--seed` draw) are collected via `_collect_and_flush` and
       merged onto what the cache already holds.

    The returned `CollectResult` carries its own `n_universe`/
    `excluded_by_result`, so a cached run reports the same universe and
    the same survivorship as the run that filled the cache.

    The settled universe itself is separately cached (Change 3,
    2026-09-07) via `_universe_or_load`, keyed to the same `--cache`
    path -- see its docstring for the walk/load/refresh decision and
    `UniverseCache.collected_at` for the staleness this introduces. That
    cache is orthogonal to this function's three cases above: even case
    1 (no candle cache yet) can still load a previously-walked universe,
    and even case 3 (resuming an interrupted candle collection) re-walks
    the universe only if `--refresh-universe` was passed or no matching
    universe cache exists.

    `--exclude-markets-from` (T18) and `--exclude-events-from` (T19) are
    applied to whichever universe the above produces, BEFORE the sample
    is drawn from it -- see `_exclude_collected` and `_exclude_events`.
    Neither has any bearing on which of the three cases runs; they only
    shrink the pool the sample is drawn from. `--stratify-by-close-week`
    then changes HOW that pool is drawn from (`_stratified_sample_by_
    close_week`) and prints the achieved per-week counts beside each
    week's available supply, because the achieved distribution -- not the
    requested one -- is what a reader has to be able to check.
    """
    cached: CollectResult | None = None
    already_attempted: set[str] = set()
    if args.cache:
        try:
            cached = read_cache(args.cache)
        except FileNotFoundError:
            cached = None
        else:
            accounted = (
                len(cached.markets) + len(cached.failures) + cached.n_short_history
            )
            if accounted >= cached.n_requested:
                print(
                    f"loaded {len(cached.markets)} markets from {args.cache}"
                    f" (interval={cached.interval_minutes}m,"
                    f" lookback={cached.lookback_days}d,"
                    f" universe={cached.n_universe})"
                )
                return cached
            print(
                f"resuming {args.cache}: {accounted}/{cached.n_requested}"
                " markets already accounted for"
            )
            already_attempted = {m.market_id for m in cached.markets} | {
                f.market_id for f in cached.failures
            }

    adapter = KalshiAdapter()
    try:
        universe, excluded = await _universe_or_load(
            adapter,
            min_volume=args.min_volume,
            cache_path=args.cache,
            refresh=args.refresh_universe,
        )
        exclude_from = getattr(args, "exclude_markets_from", None)
        if exclude_from:
            excluded_ids = _load_excluded_market_ids(exclude_from)
            before = len(universe)
            universe = _exclude_collected(universe, excluded_ids)
            print(
                f"excluded {before - len(universe)}/{len(excluded_ids)} markets"
                f" already collected in {exclude_from}; {len(universe)} remain"
                " in the universe to sample from"
            )
        exclude_events_from = getattr(args, "exclude_events_from", None)
        if exclude_events_from:
            excluded_events = _load_excluded_events(exclude_events_from)
            before = len(universe)
            universe = _exclude_events(universe, excluded_events)
            print(
                f"excluded {before - len(universe)} markets belonging to one of"
                f" {len(excluded_events)} events already collected in"
                f" {exclude_events_from}; {len(universe)} remain in the"
                " universe to sample from"
            )
        if getattr(args, "stratify_by_close_week", False):
            chosen = _stratified_sample_by_close_week(universe, args.sample)
            supply: dict[str, int] = {}
            for market in universe:
                supply[_close_week(market)] = supply.get(_close_week(market), 0) + 1
            taken: dict[str, int] = {}
            for market in chosen:
                taken[_close_week(market)] = taken.get(_close_week(market), 0) + 1
            print(
                f"stratified draw by close week: {len(chosen)} markets from"
                f" {len(taken)} weeks (requested {args.sample}); achieved"
                " counts, and the supply each week had:"
            )
            for week in sorted(supply):
                print(f"  {week}  drawn {taken.get(week, 0):6d}"
                      f"  of {supply[week]:6d} available")
        else:
            chosen = random.sample(universe, min(args.sample, len(universe)))
        remaining = [m for m in chosen if m.market_id not in already_attempted]
        collected = await _collect_and_flush(
            adapter,
            remaining,
            interval_minutes=args.interval,
            lookback_days=args.days,
            concurrency=args.concurrency,
            cache_path=args.cache,
            n_requested=len(chosen),
            seed=cached,
        )
    finally:
        await adapter.aclose()
    print(
        f"collected {len(collected.markets)}/{collected.n_requested} markets"
        f" ({collected.n_short_history} too short,"
        f" {collected.n_payload_errors} payload errors,"
        f" {collected.n_request_errors} request errors)"
    )
    collected = replace(
        collected, n_universe=len(universe), excluded_by_result=excluded
    )
    if args.cache:
        write_cache(args.cache, collected)
    return collected


async def _main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # `KalshiAdapter` logs an unknown market status or a skipped payload
    # entry as `logger.warning("venue", extra={...})`, and without a
    # formatter installed those reach `logging.lastResort` as the bare
    # word `venue` -- interleaved into the middle of a report whose
    # stdout IS the deliverable, with every diagnostic field thrown
    # away. `configure_logging` puts them on STDERR as structured JSON
    # carrying the reason, which is where a warning about the venue's
    # payload shape belongs while the report keeps stdout to itself.
    configure_logging(level="WARNING")
    random.seed(args.seed)

    collected = await _collect_or_load(args)
    markets = collected.markets
    if not markets:
        print("no markets collected; nothing to replay", file=sys.stderr)
        return 1
    if collected.failure_rate > MAX_COLLECT_FAILURE_RATE:
        # A wall of failures is the venue's payload shape having moved
        # under T1's parser, not a lossy run. Reporting it as clean is
        # the failure this check exists to prevent.
        print(
            f"collection failure rate {collected.failure_rate:.4f} exceeds"
            f" {MAX_COLLECT_FAILURE_RATE:.4f}; refusing to report this run"
            " as clean. First failures:",
            file=sys.stderr,
        )
        for failure in collected.failures[:5]:
            print(f"  {failure.market_id} [{failure.kind}] {failure.detail}",
                  file=sys.stderr)
        return 1

    policy = MarketMaker(
        min_spread=args.min_spread,
        edge_fraction=args.edge_fraction,
        max_inventory=args.max_inventory,
        quote_size=args.quote_size,
        skew_strength=args.skew_strength,
    )
    cutoff, cutoff_source = _cutoff_ts(markets, args.temporal_cutoff)
    fee_model = KalshiFeeModel()
    schedule = default_kalshi_schedule()

    closes = sorted(m.close_ts for m in markets)
    payload: dict[str, Any] = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "data_window": {
            "venue": "kalshi",
            "interval_minutes": collected.interval_minutes or args.interval,
            "lookback_days": collected.lookback_days or args.days,
            "first_close_ts": closes[0],
            "last_close_ts": closes[-1],
            "first_close": datetime.fromtimestamp(closes[0], tz=UTC).isoformat(),
            "last_close": datetime.fromtimestamp(closes[-1], tz=UTC).isoformat(),
            "n_markets": len(markets),
            "n_candles": sum(len(m.candles) for m in markets),
            "cutoff_ts": cutoff,
            "cutoff_source": cutoff_source,
            "cutoff": datetime.fromtimestamp(cutoff, tz=UTC).isoformat(),
        },
        "collection": {
            **collected.summary(),
            "min_volume": args.min_volume,
        },
        "policy": {
            "min_spread": policy.min_spread,
            "edge_fraction": policy.edge_fraction,
            "max_inventory": policy.max_inventory,
            "skew_strength": policy.skew_strength,
            "quote_size": policy.quote_size,
            "tick_size": 0.01,
            "fee_model": type(fee_model).__name__,
            "maker_rate": schedule.maker_rate,
            "taker_rate": schedule.taker_rate,
            "maker_rebate_rate": schedule.maker_rebate_rate,
            "fee_source": schedule.source,
            "seed": args.seed,
        },
    }
    # Pessimistic FIRST (GUARDRAILS.md §2.2): the verdict is computed on
    # it, and the reading order is part of not overstating the result.
    for fill_model in ("pessimistic", "optimistic"):
        result = replay(
            markets,
            policy=policy,
            fill_model=fill_model,
            fee_model=fee_model,
            schedule=schedule,
        )
        payload[fill_model] = report(
            result, split="temporal", cutoff_ts=cutoff, seed=args.seed
        )

    print_report(payload)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, allow_nan=False)
        print(f"\nwrote {args.out}")
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    sys.exit(main())
