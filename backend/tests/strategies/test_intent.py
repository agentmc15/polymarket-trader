"""Tests for `Leg`/`Intent` (T06, PLAN.md D7).

Derived from the T06 brief/acceptance criteria in
`.claude/kits/market-edge/TASKS.md`, not from reading `app/strategies/base.py`
and mirroring its implementation.
"""
from datetime import datetime, timedelta
from typing import Any

import pytest

from app.services.backtesting import BacktestConfig, Backtester, InMemoryDataReplayer
from app.strategies.base import (
    BaseStrategy,
    Intent,
    Leg,
    MarketSnapshot,
    Signal,
    SignalType,
)
from app.utils.time import utcnow
from tests.helpers import make_snapshot

# ---------------------------------------------------------------------------
# `Leg`: exactly one of size_contracts/size_usd may be set at construction.
# ---------------------------------------------------------------------------


def test_leg_both_sizes_none_is_allowed() -> None:
    """A `Leg` may leave both sizes `None` — sized later by
    `calculate_position_size`.
    """
    leg = Leg(market_id="m1", outcome="YES", side="BUY", limit_price=0.5)
    assert leg.size_contracts is None
    assert leg.size_usd is None


def test_leg_size_contracts_only_is_allowed() -> None:
    leg = Leg(
        market_id="m1", outcome="YES", side="BUY", limit_price=0.5, size_contracts=10.0
    )
    assert leg.size_contracts == 10.0
    assert leg.size_usd is None


def test_leg_size_usd_only_is_allowed() -> None:
    leg = Leg(market_id="m1", outcome="YES", side="BUY", limit_price=0.5, size_usd=25.0)
    assert leg.size_usd == 25.0
    assert leg.size_contracts is None


def test_leg_both_sizes_set_raises() -> None:
    with pytest.raises(ValueError):
        Leg(
            market_id="m1",
            outcome="YES",
            side="BUY",
            limit_price=0.5,
            size_contracts=10.0,
            size_usd=25.0,
        )


def test_leg_venue_defaults_to_polymarket() -> None:
    leg = Leg(market_id="m1", outcome="YES", side="BUY", limit_price=0.5)
    assert leg.venue == "polymarket"


# ---------------------------------------------------------------------------
# Phase-1 remediation FIX 1: `Leg.outcome` is normalized at construction, so
# Polymarket's Gamma-verbatim "Yes"/"No" and Kalshi's forced "YES"/"NO" can
# never diverge once they reach a `Leg`.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Yes", "YES"), ("yes", "YES"), ("YES", "YES"), ("No", "NO"), ("no", "NO"), ("NO", "NO")],
)
def test_leg_normalizes_yes_no_outcome_casing(raw: str, expected: str) -> None:
    leg = Leg(market_id="m1", outcome=raw, side="BUY", limit_price=0.5)
    assert leg.outcome == expected


def test_leg_leaves_non_binary_outcome_name_untouched() -> None:
    """A multi-outcome bundle's named outcome (e.g. a candidate name) is not
    a YES/NO casing convention `normalize_outcome()` owns.
    """
    leg = Leg(market_id="m1", outcome="Trump", side="BUY", limit_price=0.2)
    assert leg.outcome == "Trump"


# ---------------------------------------------------------------------------
# `Intent`: at least one leg.
# ---------------------------------------------------------------------------


def test_intent_zero_legs_raises() -> None:
    with pytest.raises(ValueError):
        Intent(
            kind="single",
            legs=[],
            hold_to_resolution=False,
            atomicity="best_effort",
            confidence=0.9,
        )


def test_intent_single_one_leg_is_valid() -> None:
    leg = Leg(market_id="m1", outcome="YES", side="BUY", limit_price=0.5)
    intent = Intent(
        kind="single",
        legs=[leg],
        hold_to_resolution=False,
        atomicity="best_effort",
        confidence=0.9,
    )
    assert intent.legs == [leg]


# ---------------------------------------------------------------------------
# `Intent(kind="complement")`: exactly 2 legs, same venue+market, outcomes
# {YES, NO}.
# ---------------------------------------------------------------------------


def _complement_legs(
    outcome_a: str = "YES",
    outcome_b: str = "NO",
    venue_a: str = "polymarket",
    venue_b: str = "polymarket",
    market_a: str = "m1",
    market_b: str = "m1",
) -> list[Leg]:
    return [
        Leg(
            market_id=market_a,
            outcome=outcome_a,
            side="BUY",
            limit_price=0.4,
            venue=venue_a,  # type: ignore[arg-type]
        ),
        Leg(
            market_id=market_b,
            outcome=outcome_b,
            side="BUY",
            limit_price=0.55,
            venue=venue_b,  # type: ignore[arg-type]
        ),
    ]


def test_complement_valid_two_legs_same_venue_market_yes_no() -> None:
    intent = Intent(
        kind="complement",
        legs=_complement_legs(),
        hold_to_resolution=True,
        atomicity="all_or_none",
        confidence=1.0,
    )
    assert intent.kind == "complement"
    assert len(intent.legs) == 2


def test_complement_wrong_leg_count_raises() -> None:
    leg = Leg(market_id="m1", outcome="YES", side="BUY", limit_price=0.4)
    with pytest.raises(ValueError):
        Intent(
            kind="complement",
            legs=[leg],
            hold_to_resolution=True,
            atomicity="all_or_none",
            confidence=1.0,
        )


def test_complement_different_venues_raises() -> None:
    with pytest.raises(ValueError):
        Intent(
            kind="complement",
            legs=_complement_legs(venue_a="polymarket", venue_b="kalshi"),
            hold_to_resolution=True,
            atomicity="all_or_none",
            confidence=1.0,
        )


def test_complement_different_markets_raises() -> None:
    with pytest.raises(ValueError):
        Intent(
            kind="complement",
            legs=_complement_legs(market_a="m1", market_b="m2"),
            hold_to_resolution=True,
            atomicity="all_or_none",
            confidence=1.0,
        )


def test_complement_outcomes_not_yes_no_raises() -> None:
    with pytest.raises(ValueError):
        Intent(
            kind="complement",
            legs=_complement_legs(outcome_a="YES", outcome_b="YES"),
            hold_to_resolution=True,
            atomicity="all_or_none",
            confidence=1.0,
        )


# ---------------------------------------------------------------------------
# `Intent(kind="cross_venue")`: exactly 2 legs, different venues.
# ---------------------------------------------------------------------------


def test_cross_venue_valid_two_legs_different_venues() -> None:
    intent = Intent(
        kind="cross_venue",
        legs=[
            Leg(market_id="m1", outcome="YES", side="BUY", limit_price=0.4, venue="polymarket"),
            Leg(market_id="m2", outcome="NO", side="BUY", limit_price=0.5, venue="kalshi"),
        ],
        hold_to_resolution=True,
        atomicity="all_or_none",
        confidence=0.8,
    )
    assert intent.kind == "cross_venue"


def test_cross_venue_same_venue_raises() -> None:
    with pytest.raises(ValueError):
        Intent(
            kind="cross_venue",
            legs=[
                Leg(market_id="m1", outcome="YES", side="BUY", limit_price=0.4, venue="polymarket"),
                Leg(market_id="m2", outcome="NO", side="BUY", limit_price=0.5, venue="polymarket"),
            ],
            hold_to_resolution=True,
            atomicity="all_or_none",
            confidence=0.8,
        )


def test_cross_venue_wrong_leg_count_raises() -> None:
    with pytest.raises(ValueError):
        Intent(
            kind="cross_venue",
            legs=[
                Leg(market_id="m1", outcome="YES", side="BUY", limit_price=0.4, venue="polymarket"),
                Leg(market_id="m2", outcome="NO", side="BUY", limit_price=0.5, venue="kalshi"),
                Leg(market_id="m3", outcome="NO", side="BUY", limit_price=0.5, venue="kalshi"),
            ],
            hold_to_resolution=True,
            atomicity="all_or_none",
            confidence=0.8,
        )


def test_cross_venue_outcomes_not_yes_no_raises() -> None:
    """Phase-1 remediation FIX 1: `cross_venue` used to have NO outcome
    check at all (unlike `complement`, which requires exactly
    `{"YES", "NO"}`), so two YES legs on two venues constructed without
    complaint.
    """
    with pytest.raises(ValueError):
        Intent(
            kind="cross_venue",
            legs=[
                Leg(market_id="m1", outcome="YES", side="BUY", limit_price=0.4, venue="polymarket"),
                Leg(market_id="m2", outcome="YES", side="BUY", limit_price=0.5, venue="kalshi"),
            ],
            hold_to_resolution=True,
            atomicity="all_or_none",
            confidence=0.8,
        )


def test_cross_venue_mixed_case_outcomes_are_valid() -> None:
    """A Polymarket-verbatim `"Yes"` leg paired with a Kalshi `"NO"` leg is
    a valid complement once `Leg.outcome` normalizes casing — the outcome
    check reads the NORMALIZED value, not the raw one either venue typed.
    """
    intent = Intent(
        kind="cross_venue",
        legs=[
            Leg(market_id="m1", outcome="Yes", side="BUY", limit_price=0.4, venue="polymarket"),
            Leg(market_id="m2", outcome="NO", side="BUY", limit_price=0.5, venue="kalshi"),
        ],
        hold_to_resolution=True,
        atomicity="all_or_none",
        confidence=0.8,
    )
    assert {leg.outcome for leg in intent.legs} == {"YES", "NO"}


# ---------------------------------------------------------------------------
# `Intent(kind="bundle")`: at least 3 legs, same market, distinct outcomes.
# ---------------------------------------------------------------------------


def _bundle_legs(n: int = 3, market_id: str = "m1", venue: str = "polymarket") -> list[Leg]:
    return [
        Leg(
            market_id=market_id,
            outcome=f"OUTCOME_{i}",
            side="BUY",
            limit_price=0.2,
            venue=venue,  # type: ignore[arg-type]
        )
        for i in range(n)
    ]


def test_bundle_valid_three_legs_same_market_distinct_outcomes() -> None:
    intent = Intent(
        kind="bundle",
        legs=_bundle_legs(3),
        hold_to_resolution=True,
        atomicity="best_effort",
        confidence=0.7,
    )
    assert intent.kind == "bundle"
    assert len(intent.legs) == 3


def test_bundle_fewer_than_three_legs_raises() -> None:
    with pytest.raises(ValueError):
        Intent(
            kind="bundle",
            legs=_bundle_legs(2),
            hold_to_resolution=True,
            atomicity="best_effort",
            confidence=0.7,
        )


def test_bundle_different_markets_raises() -> None:
    legs = _bundle_legs(3)
    legs[-1] = Leg(market_id="m2", outcome="OUTCOME_2", side="BUY", limit_price=0.2)
    with pytest.raises(ValueError):
        Intent(
            kind="bundle",
            legs=legs,
            hold_to_resolution=True,
            atomicity="best_effort",
            confidence=0.7,
        )


def test_bundle_duplicate_outcome_raises() -> None:
    legs = _bundle_legs(3)
    legs[-1] = Leg(market_id="m1", outcome=legs[0].outcome, side="BUY", limit_price=0.2)
    with pytest.raises(ValueError):
        Intent(
            kind="bundle",
            legs=legs,
            hold_to_resolution=True,
            atomicity="best_effort",
            confidence=0.7,
        )


# ---------------------------------------------------------------------------
# `expected_resolution_ts` aware or None.
# ---------------------------------------------------------------------------


def test_intent_expected_resolution_ts_none_is_valid() -> None:
    leg = Leg(market_id="m1", outcome="YES", side="BUY", limit_price=0.5)
    intent = Intent(
        kind="single",
        legs=[leg],
        hold_to_resolution=False,
        atomicity="best_effort",
        confidence=0.5,
        expected_resolution_ts=None,
    )
    assert intent.expected_resolution_ts is None


def test_intent_expected_resolution_ts_aware_is_valid() -> None:
    leg = Leg(market_id="m1", outcome="YES", side="BUY", limit_price=0.5)
    ts = utcnow() + timedelta(days=1)
    intent = Intent(
        kind="single",
        legs=[leg],
        hold_to_resolution=False,
        atomicity="best_effort",
        confidence=0.5,
        expected_resolution_ts=ts,
    )
    assert intent.expected_resolution_ts == ts


def test_intent_expected_resolution_ts_naive_raises() -> None:
    leg = Leg(market_id="m1", outcome="YES", side="BUY", limit_price=0.5)
    naive_ts = datetime(2026, 1, 1, 12, 0, 0)
    with pytest.raises(ValueError):
        Intent(
            kind="single",
            legs=[leg],
            hold_to_resolution=False,
            atomicity="best_effort",
            confidence=0.5,
            expected_resolution_ts=naive_ts,
        )


# ---------------------------------------------------------------------------
# `confidence` in [0, 1].
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0])
def test_intent_confidence_in_range_is_valid(confidence: float) -> None:
    leg = Leg(market_id="m1", outcome="YES", side="BUY", limit_price=0.5)
    intent = Intent(
        kind="single",
        legs=[leg],
        hold_to_resolution=False,
        atomicity="best_effort",
        confidence=confidence,
    )
    assert intent.confidence == confidence


@pytest.mark.parametrize("confidence", [-0.01, 1.01, -1.0, 2.0])
def test_intent_confidence_out_of_range_raises(confidence: float) -> None:
    leg = Leg(market_id="m1", outcome="YES", side="BUY", limit_price=0.5)
    with pytest.raises(ValueError):
        Intent(
            kind="single",
            legs=[leg],
            hold_to_resolution=False,
            atomicity="best_effort",
            confidence=confidence,
        )


# ---------------------------------------------------------------------------
# `Signal.to_intent()` round trip.
# ---------------------------------------------------------------------------


def test_to_intent_buy_round_trip() -> None:
    signal = Signal(
        type=SignalType.BUY,
        market_id="m1",
        token_id="m1_yes",
        outcome="YES",
        price=0.42,
        size=37.5,
        confidence=0.83,
        metadata={"reason": "test"},
    )
    intent = signal.to_intent()

    assert intent.kind == "single"
    assert intent.hold_to_resolution is False
    assert intent.atomicity == "best_effort"
    assert intent.confidence == 0.83
    assert intent.metadata == {"reason": "test"}
    assert len(intent.legs) == 1

    leg = intent.legs[0]
    assert leg.market_id == "m1"
    assert leg.outcome == "YES"
    assert leg.side == "BUY"
    assert leg.limit_price == 0.42
    assert leg.size_usd == 37.5
    assert leg.size_contracts is None
    assert leg.venue == "polymarket"


def test_to_intent_sell_maps_side() -> None:
    signal = Signal(
        type=SignalType.SELL,
        market_id="m1",
        token_id="m1_yes",
        outcome="YES",
        price=0.6,
        size=10.0,
        confidence=0.5,
    )
    intent = signal.to_intent()
    assert intent.legs[0].side == "SELL"


def test_to_intent_defaults_venue_polymarket_and_no_resolution_ts() -> None:
    """Back-compat: a caller with no snapshot in hand (e.g. this test)
    gets the pre-FIX-2 defaults unchanged.
    """
    signal = Signal(
        type=SignalType.BUY,
        market_id="m1",
        token_id="m1_yes",
        outcome="YES",
        price=0.42,
        size=37.5,
        confidence=0.83,
    )
    intent = signal.to_intent()
    assert intent.legs[0].venue == "polymarket"
    assert intent.expected_resolution_ts is None


def test_to_intent_injects_venue_and_resolution_ts_when_supplied() -> None:
    """Phase-1 remediation FIX 2/FIX 4: a caller that DOES have the
    triggering snapshot (the backtester) passes its venue and end_date
    through explicitly, rather than every leg silently defaulting to
    `"polymarket"` regardless of which venue produced the signal.
    """
    signal = Signal(
        type=SignalType.BUY,
        market_id="k1",
        token_id="k1_yes",
        outcome="YES",
        price=0.42,
        size=37.5,
        confidence=0.83,
    )
    resolution_ts = utcnow() + timedelta(days=3)
    intent = signal.to_intent(venue="kalshi", expected_resolution_ts=resolution_ts)
    assert intent.legs[0].venue == "kalshi"
    assert intent.expected_resolution_ts == resolution_ts


def test_to_intent_hold_raises() -> None:
    signal = Signal(
        type=SignalType.HOLD,
        market_id="m1",
        token_id="m1_yes",
        outcome="YES",
        price=0.5,
        size=0.0,
        confidence=0.5,
    )
    with pytest.raises(ValueError):
        signal.to_intent()


# ---------------------------------------------------------------------------
# `MarketSnapshot` with a naive `timestamp` raises (PLAN.md R9 tripwire).
# `make_snapshot` is NOT used here — a test that wants a naive value
# constructs `MarketSnapshot` directly (see `tests/helpers.py::make_snapshot`
# docstring for why).
# ---------------------------------------------------------------------------


def test_market_snapshot_naive_timestamp_raises() -> None:
    naive_ts = datetime(2026, 1, 1, 12, 0, 0)
    assert naive_ts.tzinfo is None

    with pytest.raises(ValueError, match="naive datetime"):
        MarketSnapshot(
            market_id="m1",
            token_id="m1_yes",
            timestamp=naive_ts,
            yes_price=0.5,
            no_price=0.5,
        )


def test_market_snapshot_aware_timestamp_is_valid() -> None:
    snapshot = make_snapshot()
    assert snapshot.timestamp.tzinfo is not None


def test_market_snapshot_venue_and_book_defaults() -> None:
    snapshot = make_snapshot()
    assert snapshot.venue == "polymarket"
    assert snapshot.book is None


# ---------------------------------------------------------------------------
# Engine: a multi-leg `Intent` is executed as MULTI-LEG (T08). The pre-T08
# bridge that executed only `intent.legs[0]` — and the warning that
# announced it — are gone; a stale warning that can no longer fire is
# worse than none, so this asserts its absence.
# ---------------------------------------------------------------------------


class _ComplementIntentStrategy(BaseStrategy):
    """Minimal strategy that emits one 2-leg complement `Intent`, then
    nothing, so the engine only ever sees one multi-leg intent.
    """

    name = "test_complement_intent_strategy"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._emitted = False

    def on_market_data(self, snapshot: MarketSnapshot) -> Intent | None:
        if self._emitted:
            return None
        self._emitted = True
        return Intent(
            kind="complement",
            legs=[
                Leg(market_id=snapshot.market_id, outcome="YES", side="BUY", limit_price=0.4),
                Leg(market_id=snapshot.market_id, outcome="NO", side="BUY", limit_price=0.55),
            ],
            hold_to_resolution=True,
            atomicity="all_or_none",
            confidence=0.9,
        )

    def calculate_position_size(
        self,
        signal: Signal,  # noqa: ARG002
        portfolio_value: float,  # noqa: ARG002
        positions: dict[str, Any],  # noqa: ARG002
    ) -> float:
        """Always return 0 — this test only cares about the absence of the
        pre-T08 warning, not about actual execution (see
        `tests/backtesting/test_engine_multileg.py` for that)."""
        return 0.0


@pytest.mark.asyncio
async def test_multi_leg_intent_does_not_log_pre_t08_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T08 removed the single-leg bridge, so its warning must be gone.

    Before T08 the engine executed only `intent.legs[0]` and logged
    `"multi-leg intent executed as single leg (pre-T08)"` so the silent
    conversion of a hedge into a directional bet was at least visible.
    T08 executes every leg (`Backtester._execute_intent`), so that
    warning no longer describes anything the engine does — and a warning
    that can never fire is worse than no warning at all, because a reader
    grepping the logs for it concludes the case never arises.
    """
    config = BacktestConfig(
        start_date=utcnow(),
        end_date=utcnow() + timedelta(hours=1),
        initial_capital=10_000.0,
    )
    strategy = _ComplementIntentStrategy()
    backtester = Backtester(config, strategy)
    snapshot = make_snapshot(ts=utcnow())
    replayer = InMemoryDataReplayer([snapshot])

    with caplog.at_level("WARNING", logger="app.services.backtesting.engine"):
        result = await backtester.run(replayer)

    assert result.signals_generated == 1
    assert result.intents_generated == 1

    stale = [
        r
        for r in caplog.records
        if "multi-leg intent executed as single leg" in r.message
    ]
    assert not stale, f"pre-T08 single-leg warning still fires: {stale}"

    # The bridge itself is gone from the module, not merely unused.
    from app.services.backtesting import engine as engine_module

    assert not hasattr(engine_module, "_leg_to_signal")


@pytest.mark.asyncio
async def test_single_leg_signal_does_not_log_multi_leg_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A plain one-leg `Signal` (normalized via `to_intent()`) must NOT
    trigger the multi-leg warning — it should fire only for genuinely
    multi-leg intents.
    """
    from app.strategies.favorite_compounder import FavoriteCompounderStrategy

    config = BacktestConfig(
        start_date=utcnow(),
        end_date=utcnow() + timedelta(hours=1),
        initial_capital=10_000.0,
    )
    strategy = FavoriteCompounderStrategy()
    backtester = Backtester(config, strategy)
    snapshot = make_snapshot(ts=utcnow(), yes=0.9)
    replayer = InMemoryDataReplayer([snapshot])

    with caplog.at_level("WARNING", logger="app.services.backtesting.engine"):
        await backtester.run(replayer)

    warnings = [
        r
        for r in caplog.records
        if "multi-leg intent executed as single leg" in r.message
    ]
    assert not warnings
