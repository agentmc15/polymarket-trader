"""Sweep `MarketMaker`'s calibrated parameters, out of sample, per PLAN.md D7.

WHAT THIS ANSWERS. T3's Gate 1 replay (`app.scripts.mm_backtest`) found the
THEN-shipped defaults (`edge_fraction=0.80`, `min_spread=0.10`,
`max_inventory=20.0`) positive in-sample (overall test CI
[+0.1622, +0.4314]) and NOT out of sample (temporal-test CI
[-0.1205, +0.4507]) -- the effect halves between train and test. This
module asks the only useful next question: is there a DIFFERENT point in
the same three-parameter grid that survives out of sample where the
then-shipped defaults do not? `edge_fraction ∈ {0.6,0.7,0.8,0.9}`,
`min_spread ∈ {0.05,0.10,0.15,0.25}`, `max_inventory ∈ {10,20,50}` -- 48
points including the then-shipped combination itself.

CURRENT STATE, as of 2026-09-08: `market_making.py`'s defaults moved to
`edge_fraction=0.90`, `min_spread=0.25`, `max_inventory=50.0` on the
strength of a later 1-minute holdout report. A Phase 1 review of that
holdout found it event-contaminated and its temporal window a single
1.70-day holiday weekend (see `app.strategies.market_making`'s module
docstring) -- the change is NOT certified by the two-split rule this
module implements, which is why the historical numbers above (T3's
in-sample/out-of-sample split on the ORIGINAL 0.80/0.10/20.0 defaults)
are left as originally measured rather than restated for the new values.

THE MOTIVATING NUMBER (recorded because it sets the prior this sweep
tests rather than assumes): 3,390 of 4,320 trading markets in T3's
overall block (78.5%) carried inventory into settlement, and 1,414
settled short into `yes` -- a coin flip with no edge in it, entering the
cash P&L at `inventory * (settle - last_mid)`. `max_inventory` is the
policy's only lever on the SIZE of that coin flip (PLAN.md D7's
rationale is about `min_spread`/`edge_fraction`; the inventory-vs-skew
substitution is `market_making.py`'s own `DEFAULT_MAX_INVENTORY`
docstring). This module MEASURES whether tightening it buys out-of-sample
stability; it does not assume the answer, and the report states the
`held_into_settlement` share this sweep actually measured at each grid
value alongside the two-split verdict.

THE TWO-SPLIT RULE IS SHARED MACHINERY, NOT A T4 PRIVATE HELPER. PLAN.md
"A default changes on thin evidence": no `MarketMaker` default moves
unless a challenger beats the current defaults on >= 90% of 60 independent
random EVENT-halves (never market-halves -- T2's red team found the power
table treating one 49-market event as 49 independent draws, and a market
-level half-split here would reproduce that error one layer up) AND on
the temporal hold-out. `two_split_rule()` below is exactly that rule,
built generically over `Mapping[K, ReplayResult]` so T6's taper sweep
(`taper_hours` values, not this module's 3-tuple) can call it unchanged
rather than re-deriving the 90%-and-temporal gate a second time.

WHY THE SWEEP DOES NOT CALL `report()` PER HALF. `report()`'s `_block()`
computes a 500-replicate cluster-bootstrap CI and a 4-size power table
(another 2,000 resamples) on every call -- ~1.8s against the live
15,283-market cache (measured). This sweep needs `objective()` at 48
grid points x 60 halves x (tune + score) per scope, eleven scopes
(venue-wide plus the ten largest series) -- `report()` at that volume is
hours, not minutes. `_return_on_capital()` below is not a second
implementation of the P&L bookkeeping (that bookkeeping happened once,
inside `replay()`, before this module ever runs): it is the same `roc`
arithmetic `_block()` already publishes
(`total_pnl / (n_quoted * collateral_mean)`), read back from `MarketRow`
-- `report()`'s own public output type -- rather than recomputed from
candles. The bootstrap-bearing `report()` is still what this module calls
to produce every number that actually appears in the published report,
for exactly the policy this sweep settles on, once per scope -- never
skipped, never approximated there.

WHY A CONSTANT-COST TUNE STEP IS AFFORDABLE HERE AND WAS NOT ASSUMED TO
BE. `replay()` has no cross-market state -- each `MarketRow` depends only
on its own market's candles and the policy, never on which other markets
are in the batch. So replaying the FULL scope once per grid point (48
calls) and then partitioning the resulting `MarketRow`s by event for each
of 60 halves is IDENTICAL to re-replaying a half-sized market list 60
times, at a fraction of the cost -- partitioning already-computed rows is
a dict-and-sort operation, not a second pass over candles. This lets the
"tune" step be a genuine full-grid argmax on every half (not a fixed
challenger asserted in advance) at the same 48-replay cost as evaluating
one candidate the naive way.

FILL MODEL. The search objective is computed under `fill_model=
"pessimistic"` only (GUARDRAILS.md §2.2: the verdict is computed on the
conservative model). The optimistic model is replayed and reported
BESIDE the chosen policy's numbers in the final report, exactly once per
scope, and never decides anything (PLAN.md D3).

READ-ONLY, OFFLINE. This module opens no venue connection at all -- it
reads a `--cache` file `app.scripts.mm_backtest.read_cache` already wrote
(T3's `backend/.cache/mm/kalshi-60m.json`) and calls only `replay()`/
`report()` from that module. It has no code path that could place,
modify or cancel an order (GUARDRAILS.md §1.1).
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar

from app.execution.passive_fill import FillModel
from app.scripts.mm_backtest import (
    DEFAULT_QUOTE_SIZE,
    DEFAULT_SEED,
    MarketCandles,
    MarketRow,
    ReplayResult,
    _split,
    read_cache,
    replay,
    report,
)
from app.strategies.market_making import (
    DEFAULT_EDGE_FRACTION,
    DEFAULT_MAX_INVENTORY,
    DEFAULT_MIN_SPREAD,
    DEFAULT_SKEW_STRENGTH,
    MarketMaker,
)

#: Bootstrap/tuning seed. Same value as `mm_backtest.DEFAULT_SEED` so a
#: report produced here is reproducible from `--seed` alone, exactly as
#: that module's own convention promises.
__all__ = [
    "DEFAULT_PARAMS",
    "EDGE_FRACTION_GRID",
    "MAX_INVENTORY_GRID",
    "MIN_SPREAD_GRID",
    "N_HALVES",
    "WIN_THRESHOLD",
    "MaxInventorySlice",
    "PolicyParams",
    "SweepResult",
    "TwoSplitResult",
    "grid",
    "policy_for",
    "seventy_percent_cutoff",
    "sweep",
    "two_split_rule",
]

#: T4's brief, verbatim: the three-parameter grid, 4x4x3 = 48 points
#: including the combination that was shipped when this grid was built,
#: `(0.80, 0.10, 20.0)`. It is NOT the current shipped combination --
#: `market_making.py`'s defaults moved to `(0.90, 0.25, 50.0)` on
#: 2026-09-08, on a holdout report a Phase 1 review found event-
#: contaminated and NOT certified by the two-split rule this module
#: implements (see `app.strategies.market_making`'s module docstring).
#: `skew_strength` and `quote_size` are held at
#: their current defaults -- T5 owns the first (hourly candles cannot see
#: intra-hour inventory swings, so it needs 1-minute data this module
#: does not have) and an operator's portfolio size owns the second.
EDGE_FRACTION_GRID: tuple[float, ...] = (0.6, 0.7, 0.8, 0.9)
MIN_SPREAD_GRID: tuple[float, ...] = (0.05, 0.10, 0.15, 0.25)
MAX_INVENTORY_GRID: tuple[float, ...] = (10.0, 20.0, 50.0)

#: 60 independent random halves and a 90% win rate: PLAN.md "A default
#: changes on thin evidence" verbatim. Not tuned here -- moving either
#: number to make a challenger pass would be exactly the threshold-
#: lowering GUARDRAILS.md §2.6 forbids applied one level up.
N_HALVES = 60
WIN_THRESHOLD = 0.90

#: Series ranked by `n_trading` under the CURRENT defaults; only the top
#: this many get a per-series sweep (T4's brief: "each of the 10 largest
#: `series`").
TOP_N_SERIES = 10


@dataclass(frozen=True)
class PolicyParams:
    """One point in the three-parameter grid this sweep tests.

    Hashable and comparable by value, so it can key a
    `dict[PolicyParams, ReplayResult]` and travel through
    `two_split_rule()` unmodified for any grid a future task defines
    (T6 uses a plain `float` key for `taper_hours` instead -- see that
    task's brief).
    """

    edge_fraction: float
    min_spread: float
    max_inventory: float


#: The policy actually shipped in `app/strategies/market_making.py`
#: today. This IS one of the 48 grid points constructed by `grid()` (each
#: axis's defaults are also its own value), so `sweep()` never has to
#: replay it twice.
DEFAULT_PARAMS = PolicyParams(
    edge_fraction=DEFAULT_EDGE_FRACTION,
    min_spread=DEFAULT_MIN_SPREAD,
    max_inventory=DEFAULT_MAX_INVENTORY,
)


def grid() -> tuple[PolicyParams, ...]:
    """The full Cartesian sweep, `DEFAULT_PARAMS` included.

    Returns:
        tuple[PolicyParams, ...]: 48 points,
            `len(EDGE_FRACTION_GRID) * len(MIN_SPREAD_GRID) *
            len(MAX_INVENTORY_GRID)`.
    """
    return tuple(
        PolicyParams(edge_fraction=e, min_spread=s, max_inventory=m)
        for e in EDGE_FRACTION_GRID
        for s in MIN_SPREAD_GRID
        for m in MAX_INVENTORY_GRID
    )


def policy_for(
    params: PolicyParams,
    *,
    skew_strength: float = DEFAULT_SKEW_STRENGTH,
    quote_size: float = DEFAULT_QUOTE_SIZE,
) -> MarketMaker:
    """Build the `MarketMaker` one grid point names."""
    return MarketMaker(
        edge_fraction=params.edge_fraction,
        min_spread=params.min_spread,
        max_inventory=params.max_inventory,
        skew_strength=skew_strength,
        quote_size=quote_size,
    )


def _return_on_capital(rows: Sequence[MarketRow]) -> float | None:
    """`total cash pnl / (n_quoted * mean collateral per quoted market)`.

    Byte-identical to `app.scripts.mm_backtest._block`'s `roc` field --
    see the module docstring for why this is read back from `MarketRow`
    rather than obtained by calling `report()` at sweep volume. `None`
    when nothing was quoted or the denominator is non-positive, mirroring
    `_block`'s own convention: an unavailable number is `None`, never a
    substituted 0.0 (GUARDRAILS.md §7).

    Args:
        rows: Any subset of one `ReplayResult`'s rows -- a full scope, a
            random half, or a temporal split of either.

    Returns:
        float | None: Return on capital over `rows`.
    """
    quoted = [r for r in rows if r.quote_hours > 0]
    if not quoted:
        return None
    total_pnl = math.fsum(r.pnl for r in rows)
    collateral_mean = statistics.fmean(r.collateral_mean for r in quoted)
    denominator = len(quoted) * collateral_mean
    if denominator <= 0.0:
        return None
    return total_pnl / denominator


Objective = Callable[[Sequence[MarketRow]], float | None]
K = TypeVar("K")


def _passes_two_split_rule(
    *,
    wins: int,
    n_halves: int,
    temporal_win: bool | None,
    win_threshold: float = WIN_THRESHOLD,
) -> bool:
    """The kit's default-change gate, as one decision (PLAN.md "A default
    changes on thin evidence").

    A challenger needs BOTH a win rate at or above `win_threshold` over
    `n_halves` independent random draws AND a strictly passing temporal
    test -- 55 of 60 (91.7%) clears the floor and 53 of 60 (88.3%) does
    not, and a qualifying win rate is discarded outright if
    `temporal_win` is not `True` (PLAN.md D5: the random split tunes, the
    temporal split verdicts; letting 60 arbitrary halves override the one
    split built to catch regime change would undo the reason a temporal
    hold-out exists at all).

    Args:
        wins: Halves the challenger's held-out objective beat the
            defaults'.
        n_halves: Halves drawn (the rule's own denominator).
        temporal_win: Whether the SAME challenger beat the defaults on
            the temporal test split; `None` means it was never checked
            (no challenger reached this gate).
        win_threshold: The 90% floor (`WIN_THRESHOLD`).

    Returns:
        bool: Whether the challenger passes.
    """
    if n_halves <= 0:
        return False
    return (wins / n_halves) >= win_threshold and temporal_win is True


@dataclass(frozen=True)
class TwoSplitResult:
    """The kit's shared two-split calibration rule, evaluated once.

    Every `MarketMaker` default this kit moves (T4's `edge_fraction`/
    `min_spread`/`max_inventory`, T6's `taper_hours`) is decided by one
    call to `two_split_rule()`, and this is its answer.

    Attributes:
        default_key: The baseline every challenger is measured against.
        candidate_key: The BEST-performing challenger this run considered
            (by win count, tie-broken by full-sample objective), whether
            or not it actually passes -- reported even on a losing sweep
            (PLAN.md "a sweep that finds nothing is a result").
        champion_key: `candidate_key` if `passed`, else `None`. This is
            the key a caller should actually ship.
        passed: Whether `champion_key` cleared `_passes_two_split_rule`.
        n_halves: Random event-halves drawn.
        win_threshold: The rule's win-rate floor.
        wins: Halves `candidate_key` was picked by the tuning half AND
            beat the defaults on the held-out scoring half.
        win_rate: `wins / n_halves`, or `None` if `n_halves` is 0.
        times_selected_by_tuning: Halves where `candidate_key` was the
            argmax of the FULL grid on the tuning half (>= `wins`,
            because a half can select it without it then holding up on
            the scoring half).
        temporal_default_roc: Defaults' objective on the temporal test
            split (`None` if unavailable).
        temporal_candidate_roc: `candidate_key`'s objective on the same
            split.
        temporal_win: Whether the candidate beat the defaults there;
            `None` if no candidate was ever selected by a half.
        full_sample_default_roc: Defaults' objective on the WHOLE scope
            (train+test together) -- context, never the win criterion.
        full_sample_candidate_roc: `candidate_key`'s objective there.
        cutoff_ts: Temporal cutoff used for `temporal_*` fields.
        seed: Base seed; half `h` uses `seed + h`.
    """

    default_key: Any
    candidate_key: Any | None
    champion_key: Any | None
    passed: bool
    n_halves: int
    win_threshold: float
    wins: int
    win_rate: float | None
    times_selected_by_tuning: int
    temporal_default_roc: float | None
    temporal_candidate_roc: float | None
    temporal_win: bool | None
    full_sample_default_roc: float | None
    full_sample_candidate_roc: float | None
    cutoff_ts: int
    seed: int


def two_split_rule(
    candidates: Mapping[K, ReplayResult],
    *,
    default: K,
    cutoff_ts: int,
    n_halves: int = N_HALVES,
    seed: int = DEFAULT_SEED,
    win_threshold: float = WIN_THRESHOLD,
    objective: Objective = _return_on_capital,
) -> TwoSplitResult:
    """Tune on a random event-half, score on the other half AND the
    temporal test, repeated over `n_halves` draws (PLAN.md D5,
    GUARDRAILS.md §4.1).

    ONE DRAW, PRECISELY. `_split(rows, split="event", seed=half_seed)`
    (imported straight from `mm_backtest` -- PLAN.md D11, no second
    implementation) partitions events into two halves. For every key in
    `candidates`, its TUNE score is `objective` on the train half and its
    SCORE score is `objective` on the test half. The key with the highest
    TUNE score across the WHOLE grid (defaults included) is this draw's
    tuning pick; if that pick is `default` itself, the draw contributes no
    win to anyone. Otherwise the pick wins this draw iff its SCORE beats
    the defaults' SCORE on the SAME held-out half.

    WHY THIS IS A REAL GRID SEARCH, NOT A FIXED CHALLENGER ASSERTED IN
    ADVANCE, AT NO EXTRA REPLAY COST. `candidates` is expected to already
    hold one `ReplayResult` per grid point (`sweep()` builds it that way).
    Because `replay()` has no cross-market state, partitioning each
    already-computed `ReplayResult` by the SAME half boundary is
    equivalent to re-replaying a half-sized market list, so evaluating
    the full grid on every draw costs nothing beyond the one-time replay
    already paid for building `candidates` (module docstring).

    THE SAME-EVENT-SET INVARIANT THIS RELIES ON. `_split`'s event
    partition is a function of `{r.event for r in rows}` alone, and
    `replay()` emits exactly one row per input market regardless of
    policy. So calling `_split(..., seed=half_seed)` separately per key
    with an IDENTICAL event set produces an IDENTICAL train/test boundary
    for every key on that draw -- this is checked below, not assumed:
    every `ReplayResult` in `candidates` must have been built from the
    SAME market list (`sweep()` guarantees this; callers who build
    `candidates` by hand must too).

    Args:
        candidates: One `ReplayResult` per grid point, ALL replayed over
            the identical market list under the same `fill_model`.
        default: The key in `candidates` this run measures every
            challenger against.
        cutoff_ts: Temporal cutoff (PLAN.md D5's go/no-go split) --
            `train = close_ts < cutoff_ts`, `test = close_ts >= cutoff_ts`.
        n_halves: Random event-halves to draw.
        seed: Base seed; draw `h` uses `_split(..., seed=seed + h)`.
        win_threshold: The rule's win-rate floor.
        objective: The statistic every comparison is made on. Defaults to
            `_return_on_capital` -- T4's brief objective, "return on
            capital".

    Returns:
        TwoSplitResult: See its docstring.

    Raises:
        ValueError: If `default` is not a key of `candidates`, or if the
            candidates were not all replayed over the same market set
            (their rows' event sets differ) -- the invariant this
            function's per-draw partitioning depends on.
    """
    if default not in candidates:
        raise ValueError("two_split_rule: `default` must be a key of `candidates`")
    event_sets = {frozenset(r.event for r in rr.rows) for rr in candidates.values()}
    if len(event_sets) > 1:
        raise ValueError(
            "two_split_rule: candidates were not all replayed over the same "
            "market set -- every ReplayResult must share one event set so "
            "a single seed partitions every key identically"
        )

    default_rows = candidates[default].rows
    selected: Counter[K] = Counter()
    wins_by_key: Counter[K] = Counter()
    for h in range(n_halves):
        half_seed = seed + h
        tune_scores: dict[K, float | None] = {}
        score_scores: dict[K, float | None] = {}
        for key, rr in candidates.items():
            parts = _split(rr.rows, split="event", cutoff_ts=None, seed=half_seed)
            tune_scores[key] = objective(parts.train)
            score_scores[key] = objective(parts.test)
        finite_tune = {k: v for k, v in tune_scores.items() if v is not None}
        if not finite_tune:
            continue
        picked = max(finite_tune, key=lambda k: finite_tune[k])
        if picked == default:
            continue
        selected[picked] += 1
        d_score, p_score = score_scores.get(default), score_scores.get(picked)
        if d_score is not None and p_score is not None and p_score > d_score:
            wins_by_key[picked] += 1

    candidate_key: K | None = None
    if wins_by_key:
        candidate_key = max(
            wins_by_key,
            key=lambda k: (
                wins_by_key[k],
                objective(candidates[k].rows) if objective(candidates[k].rows) is not None else float("-inf"),
            ),
        )
    wins = wins_by_key.get(candidate_key, 0) if candidate_key is not None else 0
    win_rate = wins / n_halves if n_halves > 0 else None

    temporal_default: float | None = None
    temporal_candidate: float | None = None
    temporal_win: bool | None = None
    if candidate_key is not None:
        d_parts = _split(default_rows, split="temporal", cutoff_ts=cutoff_ts, seed=seed)
        c_parts = _split(
            candidates[candidate_key].rows, split="temporal", cutoff_ts=cutoff_ts, seed=seed
        )
        temporal_default = objective(d_parts.test)
        temporal_candidate = objective(c_parts.test)
        temporal_win = (
            temporal_default is not None
            and temporal_candidate is not None
            and temporal_candidate > temporal_default
        )

    passed = candidate_key is not None and _passes_two_split_rule(
        wins=wins, n_halves=n_halves, temporal_win=temporal_win, win_threshold=win_threshold
    )
    return TwoSplitResult(
        default_key=default,
        candidate_key=candidate_key,
        champion_key=candidate_key if passed else None,
        passed=passed,
        n_halves=n_halves,
        win_threshold=win_threshold,
        wins=wins,
        win_rate=win_rate,
        times_selected_by_tuning=selected.get(candidate_key, 0) if candidate_key is not None else 0,
        temporal_default_roc=temporal_default,
        temporal_candidate_roc=temporal_candidate,
        temporal_win=temporal_win,
        full_sample_default_roc=objective(default_rows),
        full_sample_candidate_roc=(
            objective(candidates[candidate_key].rows) if candidate_key is not None else None
        ),
        cutoff_ts=cutoff_ts,
        seed=seed,
    )


@dataclass(frozen=True)
class MaxInventorySlice:
    """`held_into_settlement` measured at one `max_inventory`, holding
    `edge_fraction`/`min_spread` at the CURRENT shipped defaults.

    PLAN.md D7's rationale is about `min_spread`/`edge_fraction`; this
    slice isolates the ONE lever `market_making.py`'s own
    `DEFAULT_MAX_INVENTORY` docstring already names as a substitute for
    `skew_strength` on the inventory tail -- measured here on the full
    scope (train+test), pessimistic, `terminal=settled`.
    """

    fill_model: FillModel
    terminal: str
    max_inventory: float
    n_trading: int
    held_into_settlement: int
    held_share: float | None
    settled_short_into_yes: int
    roc: float | None
    mean_pnl_per_trading_market: float | None


def _slice_for(rows: Sequence[MarketRow], *, max_inventory: float) -> MaxInventorySlice:
    trading = [r for r in rows if r.n_fills > 0]
    held = sum(1 for r in trading if r.held_into_settlement)
    return MaxInventorySlice(
        fill_model="pessimistic",
        terminal="settled",
        max_inventory=max_inventory,
        n_trading=len(trading),
        held_into_settlement=held,
        held_share=(held / len(trading) if trading else None),
        settled_short_into_yes=sum(1 for r in trading if r.settled_short_into_yes),
        roc=_return_on_capital(rows),
        mean_pnl_per_trading_market=(
            math.fsum(r.pnl for r in trading) / len(trading) if trading else None
        ),
    )


@dataclass(frozen=True)
class SweepResult:
    """One scope's (venue-wide, or one `series`) full calibration sweep.

    Attributes:
        scope: `"venue-wide"` or a `series` value.
        fill_model: The search's fill model, always `"pessimistic"`
            (module docstring).
        n_markets: Markets replayed in this scope.
        n_events: Distinct events in this scope.
        cutoff_ts: Temporal cutoff used.
        train_fraction: Measured share of THIS scope's markets with
            `close_ts < cutoff_ts` -- stated because a venue-wide cutoff
            need not land at the same fraction inside one series.
        two_split: The rule's verdict for this scope.
        max_inventory_slices: `held_into_settlement` at each
            `MAX_INVENTORY_GRID` value, defaults otherwise
            (`_slice_for`).
        full_sample_roc: Every grid point's full-scope ROC, keyed by
            `repr(PolicyParams)` for JSON safety -- context for reading
            `two_split`, never itself a criterion.
    """

    scope: str
    fill_model: FillModel
    n_markets: int
    n_events: int
    cutoff_ts: int
    train_fraction: float | None
    two_split: TwoSplitResult
    max_inventory_slices: tuple[MaxInventorySlice, ...]
    full_sample_roc: dict[str, float | None]


def sweep(
    markets: Sequence[MarketCandles],
    *,
    scope: str,
    cutoff_ts: int,
    fill_model: FillModel = "pessimistic",
    n_halves: int = N_HALVES,
    seed: int = DEFAULT_SEED,
    win_threshold: float = WIN_THRESHOLD,
) -> tuple[SweepResult, dict[PolicyParams, ReplayResult]]:
    """Replay every grid point once and run `two_split_rule` over them.

    Args:
        markets: The scope's markets (the full cache for `"venue-wide"`,
            or pre-filtered to one `series`).
        scope: Label carried onto `SweepResult.scope` and every nested
            block's `fill_model`/`terminal`.
        cutoff_ts: Temporal cutoff, shared across every scope by the CLI
            (module docstring: this module always searches under
            `fill_model="pessimistic"`).
        fill_model: Fixed to `"pessimistic"` by every caller in this kit;
            exposed as a parameter only so a test can construct a
            deliberately-losing optimistic scenario without touching the
            module constant.
        n_halves: Passed to `two_split_rule`.
        seed: Passed to `two_split_rule`.
        win_threshold: Passed to `two_split_rule`.

    Returns:
        tuple[SweepResult, dict[PolicyParams, ReplayResult]]: The summary
            and the raw per-grid-point replays, so a caller can pull the
            full bootstrap `report()` for whichever policy this scope
            settles on without replaying it again.
    """
    candidates: dict[PolicyParams, ReplayResult] = {
        params: replay(markets, policy=policy_for(params), fill_model=fill_model)
        for params in grid()
    }
    two_split = two_split_rule(
        candidates,
        default=DEFAULT_PARAMS,
        cutoff_ts=cutoff_ts,
        n_halves=n_halves,
        seed=seed,
        win_threshold=win_threshold,
    )
    default_rows = candidates[DEFAULT_PARAMS].rows
    slices = tuple(
        _slice_for(
            candidates[
                PolicyParams(
                    edge_fraction=DEFAULT_EDGE_FRACTION,
                    min_spread=DEFAULT_MIN_SPREAD,
                    max_inventory=mi,
                )
            ].rows,
            max_inventory=mi,
        )
        for mi in MAX_INVENTORY_GRID
    )
    closes = [r.close_ts for r in default_rows]
    train_fraction = (
        sum(1 for c in closes if c < cutoff_ts) / len(closes) if closes else None
    )
    n_events = len({r.event for r in default_rows})
    result = SweepResult(
        scope=scope,
        fill_model=fill_model,
        n_markets=len(markets),
        n_events=n_events,
        cutoff_ts=cutoff_ts,
        train_fraction=train_fraction,
        two_split=two_split,
        max_inventory_slices=slices,
        full_sample_roc={
            repr(params): _return_on_capital(rr.rows) for params, rr in candidates.items()
        },
    )
    return result, candidates


def seventy_percent_cutoff(
    markets: Sequence[MarketCandles], *, train_fraction: float = 0.70
) -> int:
    """The temporal cutoff PLAN.md's brief requires, computed rather than
    guessed at.

    THE TRAP THIS EXISTS TO AVOID (recorded because it already fired
    once, per the orchestrator's own correction). `mm_backtest`'s CLI
    defaults `--temporal-cutoff` to the MEDIAN close (~50/50) when the
    flag is omitted -- the right default for a small ad hoc run, the
    wrong one for a go/no-go: on T3's cache the 50/50 split's test CI is
    [+0.0807, +0.5415] (a GO) and the brief's specified ~70%-train split
    gives [-0.1205, +0.4507] (a NO-GO). This function makes "~70%" a
    measured cutoff timestamp rather than a hand-picked date: sorting
    `close_ts` and taking the value at `round(train_fraction * n)`
    reproduces T3's own manually-chosen cutoff EXACTLY on the current
    cache (`1788637226`, 69.99% train) -- verified by hand before this
    function shipped, not assumed.

    Args:
        markets: The scope this cutoff will be applied to. Passing the
            FULL venue-wide market list (not a per-series subset) is
            what every scope in this module's CLI does, so one cutoff
            timestamp is shared across venue-wide and every series
            (`SweepResult.train_fraction` then reports how that ONE
            cutoff lands inside each series, which need not be 70%).
        train_fraction: Target share of closes strictly before the
            cutoff.

    Returns:
        int: A close timestamp from `markets` itself (never an
            interpolated value), so it always corresponds to an actual
            market close.

    Raises:
        ValueError: If `markets` is empty.
    """
    if not markets:
        raise ValueError("seventy_percent_cutoff: markets must be non-empty")
    closes = sorted(m.close_ts for m in markets)
    idx = min(len(closes) - 1, round(train_fraction * len(closes)))
    return closes[idx]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _rank_series(default_rows: Sequence[MarketRow], *, top_n: int) -> list[str]:
    """The `top_n` `series` values by `n_trading` under `DEFAULT_PARAMS`.

    Ranking is fixed to the CURRENT shipped defaults regardless of what
    this run's sweep later finds, so "the 10 largest series" means the
    same thing before and after a challenger might change what trades.
    """
    counts: Counter[str] = Counter()
    for row in default_rows:
        if row.n_fills > 0 and row.series:
            counts[row.series] += 1
    return [series for series, _ in counts.most_common(top_n)]


def _params_json(params: PolicyParams) -> dict[str, float]:
    return {
        "edge_fraction": params.edge_fraction,
        "min_spread": params.min_spread,
        "max_inventory": params.max_inventory,
    }


def _two_split_json(ts: TwoSplitResult) -> dict[str, Any]:
    return {
        "default": _params_json(ts.default_key),
        "candidate": _params_json(ts.candidate_key) if ts.candidate_key else None,
        "champion": _params_json(ts.champion_key) if ts.champion_key else None,
        "passed": ts.passed,
        "n_halves": ts.n_halves,
        "win_threshold": ts.win_threshold,
        "wins": ts.wins,
        "win_rate": ts.win_rate,
        "times_selected_by_tuning": ts.times_selected_by_tuning,
        "temporal_default_roc": ts.temporal_default_roc,
        "temporal_candidate_roc": ts.temporal_candidate_roc,
        "temporal_win": ts.temporal_win,
        "full_sample_default_roc": ts.full_sample_default_roc,
        "full_sample_candidate_roc": ts.full_sample_candidate_roc,
        "cutoff_ts": ts.cutoff_ts,
        "seed": ts.seed,
    }


def _scope_json(
    scope_result: SweepResult,
    candidates: dict[PolicyParams, ReplayResult],
    *,
    cutoff_ts: int,
    seed: int,
) -> dict[str, Any]:
    final_params = scope_result.two_split.champion_key or DEFAULT_PARAMS
    pess = candidates[final_params]
    opt_policy_markets_note = (
        "replayed once more under fill_model=optimistic for the chosen "
        "policy only -- never part of the search"
    )
    return {
        "scope": scope_result.scope,
        "n_markets": scope_result.n_markets,
        "n_events": scope_result.n_events,
        "cutoff_ts": cutoff_ts,
        "train_fraction": scope_result.train_fraction,
        "final_params": _params_json(final_params),
        "final_params_is_default": final_params == DEFAULT_PARAMS,
        "two_split": _two_split_json(scope_result.two_split),
        "max_inventory_slices": [
            {
                "fill_model": s.fill_model,
                "terminal": s.terminal,
                "max_inventory": s.max_inventory,
                "n_trading": s.n_trading,
                "held_into_settlement": s.held_into_settlement,
                "held_share": s.held_share,
                "settled_short_into_yes": s.settled_short_into_yes,
                "roc": s.roc,
                "mean_pnl_per_trading_market": s.mean_pnl_per_trading_market,
            }
            for s in scope_result.max_inventory_slices
        ],
        "full_sample_roc": scope_result.full_sample_roc,
        "pessimistic": report(pess, split="temporal", cutoff_ts=cutoff_ts, seed=seed),
        "_optimistic_note": opt_policy_markets_note,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sweep MarketMaker's calibrated parameters against a "
        "T2/T3 candle cache, out of sample."
    )
    parser.add_argument("--cache", type=str, required=True,
                        help="T2/T3 candle cache (backend/.cache/mm/*.json)")
    parser.add_argument("--out", type=str, default=None,
                        help="write the full sweep JSON here")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--n-halves", type=int, default=N_HALVES)
    parser.add_argument("--win-threshold", type=float, default=WIN_THRESHOLD)
    parser.add_argument("--top-series", type=int, default=TOP_N_SERIES)
    parser.add_argument("--train-fraction", type=float, default=0.70,
                        help="target share of closes before the temporal "
                             "cutoff when --temporal-cutoff is not given")
    parser.add_argument("--temporal-cutoff", type=str, default=None,
                        help="YYYY-MM-DD; overrides --train-fraction with an "
                             "exact date")
    return parser


def _main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    collected = read_cache(args.cache)
    markets = collected.markets
    if not markets:
        print("no markets in cache; nothing to sweep", file=sys.stderr)
        return 1

    if args.temporal_cutoff is not None:
        cutoff_ts = int(
            datetime.fromisoformat(args.temporal_cutoff).replace(tzinfo=UTC).timestamp()
        )
    else:
        cutoff_ts = seventy_percent_cutoff(markets, train_fraction=args.train_fraction)
    venue_train_fraction = sum(1 for m in markets if m.close_ts < cutoff_ts) / len(markets)
    print(
        f"temporal cutoff: {datetime.fromtimestamp(cutoff_ts, tz=UTC).isoformat()}"
        f" ({venue_train_fraction:.4f} of venue-wide closes in train)"
    )

    venue_result, venue_candidates = sweep(
        markets,
        scope="venue-wide",
        cutoff_ts=cutoff_ts,
        n_halves=args.n_halves,
        seed=args.seed,
        win_threshold=args.win_threshold,
    )
    scopes: dict[str, dict[str, Any]] = {
        "venue-wide": _scope_json(
            venue_result, venue_candidates, cutoff_ts=cutoff_ts, seed=args.seed
        )
    }
    print(
        f"venue-wide: passed={venue_result.two_split.passed}"
        f" wins={venue_result.two_split.wins}/{venue_result.two_split.n_halves}"
        f" candidate={venue_result.two_split.candidate_key}"
    )

    top_series = _rank_series(venue_candidates[DEFAULT_PARAMS].rows, top_n=args.top_series)
    for series in top_series:
        series_markets = [m for m in markets if m.series == series]
        series_result, series_candidates = sweep(
            series_markets,
            scope=series,
            cutoff_ts=cutoff_ts,
            n_halves=args.n_halves,
            seed=args.seed,
            win_threshold=args.win_threshold,
        )
        scopes[series] = _scope_json(
            series_result, series_candidates, cutoff_ts=cutoff_ts, seed=args.seed
        )
        print(
            f"series={series}: n_markets={series_result.n_markets}"
            f" passed={series_result.two_split.passed}"
            f" wins={series_result.two_split.wins}/{series_result.two_split.n_halves}"
            f" candidate={series_result.two_split.candidate_key}"
        )

    payload = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "cache": args.cache,
        "seed": args.seed,
        "n_halves": args.n_halves,
        "win_threshold": args.win_threshold,
        "top_series_requested": args.top_series,
        "cutoff_ts": cutoff_ts,
        "cutoff": datetime.fromtimestamp(cutoff_ts, tz=UTC).isoformat(),
        "top_series": top_series,
        "scopes": scopes,
    }
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, allow_nan=False)
        print(f"wrote {args.out}")
    return 0


def main() -> int:
    return _main()


if __name__ == "__main__":
    sys.exit(main())
