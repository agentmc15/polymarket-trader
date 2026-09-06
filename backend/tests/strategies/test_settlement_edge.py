"""T20 -- `app.strategies.settlement_edge` (PLAN.md D10(c), R2).

Derived from the T20 brief/acceptance in `.claude/kits/market-edge/TASKS.md`,
not by reading `app/strategies/settlement_edge.py` and mirroring it back.
Every money/percentage figure is computed BY HAND in a comment before it
is asserted (GUARDRAILS.md §5).

No network, no live mode, no real order (GUARDRAILS.md §1.1/§1.2/§1.4):
every `MarketSnapshot`/`OrderBook` here is hand-built via
`tests.helpers.make_book`, never a real adapter.

REVIEW FIX (post-T20): the core arithmetic tests below build an EXPLICIT
book -- either ONE deep fill or a HUNDRED single-contract fills -- rather
than leaving `snapshot.book` `None`. `_priced_fills` (the function under
test here, indirectly) prices the fee over whatever fills a real order
would actually take, and Kalshi's per-fill $0.01 ceiling makes those two
book shapes produce materially different numbers at the same price and
size (see `app.strategies.settlement_edge`'s module docstring's "VENUE
ASYMMETRY" section). A `None` book falls back to a SINGLE fill of the
full requested size (the optimistic end) -- fine for the tests that only
care whether an intent fires at all, not fine for the ones asserting an
exact fee/annualized number, which is why those now say explicitly which
book shape they assume.
"""
from datetime import timedelta

import pytest

from app.config import settings
from app.strategies.base import MarketSnapshot
from app.strategies.settlement_edge import DEFAULT_CONFIG, SettlementEdgeStrategy
from app.utils.time import utcnow
from app.venues.types import OrderBook
from tests.helpers import make_book

KX = "kalshi"
KX_MARKET = "KXTEST-1"


def test_settings_defaults_match_this_files_hand_computed_numbers() -> None:
    """Sanity check (mirrors `test_arbitrage_intents.py`'s own guard):
    every hand-computed number below assumes these four ambient
    `Settings` defaults. If an operator ever changes one, THIS test (not
    just the strategy) fails loudly instead of the numbers below
    silently drifting out of sync with the code.
    """
    assert settings.kalshi_taker_fee_rate == pytest.approx(0.07)
    assert settings.redemption_gas_usd == pytest.approx(0.05)
    assert settings.settlement_delay_hours == pytest.approx(24.0)
    assert settings.min_viable_annualized == pytest.approx(0.05)


def _kalshi_snapshot(
    *,
    yes_price: float,
    timestamp,
    end_date,
    yes_ask: float | None = None,
    book: OrderBook | None = None,
) -> MarketSnapshot:
    """A Kalshi `MarketSnapshot` whose scheduled close has already passed.

    `yes_ask` defaults to `yes_price`. `book`, if given, is what
    `_priced_fills` actually walks; `None` (the default) means the
    strategy falls back to a SINGLE fill of the full requested size at
    the top-of-book quote -- see this file's module docstring.
    """
    return MarketSnapshot(
        market_id=KX_MARKET,
        token_id=KX_MARKET,
        timestamp=timestamp,
        yes_price=yes_price,
        no_price=1.0 - yes_price,
        yes_ask=yes_ask if yes_ask is not None else yes_price,
        end_date=end_date,
        venue=KX,
        book=book,
    )


def _one_fill_book(*, price: float, size: float = 100.0) -> OrderBook:
    """A YES book with ALL of `size` resting in ONE ask level at `price`.

    The best case for Kalshi's per-fill floor: a 100-contract order
    against this book takes exactly ONE fill.
    """
    return make_book(
        bids=[(price - 0.01, size)],
        asks=[(price, size)],
        venue=KX,
        market_id=KX_MARKET,
        outcome="YES",
    )


def _fragmented_book(*, price: float, contracts: int = 100) -> OrderBook:
    """A YES book with `contracts` separate 1-contract ask levels at `price`.

    The worst case for Kalshi's per-fill floor: a `contracts`-contract
    order against this book takes `contracts` separate fills, each
    paying the $0.01 ceiling in full.
    """
    return make_book(
        bids=[(price - 0.01, 1.0)] * contracts,
        asks=[(price, 1.0)] * contracts,
        venue=KX,
        market_id=KX_MARKET,
        outcome="YES",
    )


def test_kalshi_098_one_deep_fill_signals_with_hand_computed_annualized() -> None:
    """0.98 ask, ALL 100 contracts fillable in ONE level, 30h to
    (estimated) settlement, Kalshi's default 0.07 rate.

    fee: `model_fee = 100 * 0.07 * 0.98 * 0.02 = 0.1372`, ceiled to 6dp
    (already exact there) then ceiled to the nearest whole cent (the
    non-direct-member floor, `KalshiFeeModel`'s default) -> `$0.14`
    total for the ONE fill, i.e. `$0.0014` per contract.
    gas: `redemption_gas_usd / filled_size = 0.05 / 100 = 0.0005`/contract.
    residual: `1 - 0.98 - 0.0014 - 0.0005 = 0.0181`/contract.
    fraction: `0.0181 / 0.98 = 0.01846938775510206` (1.8469%).
    floor_hours: `max(30, settlement_delay_hours=24) = 30`.
    annualized: `0.01846938775510206 * (8760 / 30) = 5.393061224489802`
    (539.31%) -- clears `min_viable_annualized` (0.05) -> SIGNAL.
    """
    now = utcnow()
    snapshot = _kalshi_snapshot(
        yes_price=0.98,
        timestamp=now,
        end_date=now - timedelta(hours=1),
        book=_one_fill_book(price=0.98),
    )
    strategy = SettlementEdgeStrategy()

    intent = strategy.evaluate(
        snapshot,
        in_dispute_window=False,
        expected_resolution_ts=now + timedelta(hours=30),
    )

    assert intent is not None
    assert intent.kind == "single"
    assert intent.hold_to_resolution is True
    assert len(intent.legs) == 1
    leg = intent.legs[0]
    assert leg.outcome == "YES"
    assert leg.side == "BUY"
    assert leg.limit_price == pytest.approx(0.98)
    assert intent.metadata["bucket"] == "near_resolution"
    assert intent.metadata["fill_count"] == 1
    assert intent.metadata["filled_size"] == pytest.approx(100.0)
    assert intent.metadata["fee"] == pytest.approx(0.0014)
    assert intent.metadata["gas_per_contract"] == pytest.approx(0.0005)
    assert intent.metadata["edge"] == pytest.approx(0.01810000000000002)
    assert intent.metadata["net_edge_fraction"] == pytest.approx(0.01846938775510206)
    assert intent.metadata["annualized_return"] == pytest.approx(5.393061224489802)
    assert intent.metadata["hours_to_resolution"] == pytest.approx(30.0)


def test_kalshi_098_fragmented_book_still_signals() -> None:
    """Same market, but the book only offers a hundred 1-contract levels
    -- the WORST case for Kalshi's per-fill floor.

    fee: `100 * ceil_cents(1 * 0.07 * 0.98 * 0.02) = 100 * $0.01 = $1.00`
    total, i.e. `$0.01` per contract (the SAME per-fill ceiling as a
    single 1-contract order, paid a hundred times over).
    gas: unchanged, `$0.0005`/contract (the same 100 contracts, one
    redemption).
    residual: `1 - 0.98 - 0.01 - 0.0005 = 0.0095`/contract.
    fraction: `0.0095 / 0.98 = 0.009693877551020426` (0.9694%).
    annualized: `0.009693877551020426 * (8760 / 30) = 2.830612244897964`
    (283.06%) -- SMALLER than the one-deep-fill case above, but still
    comfortably clears `min_viable_annualized`: 0.98's 2-cent gross
    residual survives even this worst-case fragmentation. 0.995's
    half-cent residual, in the next test, does not.
    """
    now = utcnow()
    snapshot = _kalshi_snapshot(
        yes_price=0.98,
        timestamp=now,
        end_date=now - timedelta(hours=1),
        book=_fragmented_book(price=0.98),
    )
    strategy = SettlementEdgeStrategy()

    intent = strategy.evaluate(
        snapshot,
        in_dispute_window=False,
        expected_resolution_ts=now + timedelta(hours=30),
    )

    assert intent is not None
    assert intent.metadata["fill_count"] == 100
    assert intent.metadata["filled_size"] == pytest.approx(100.0)
    assert intent.metadata["fee"] == pytest.approx(0.01)
    assert intent.metadata["edge"] == pytest.approx(0.009500000000000017)
    assert intent.metadata["net_edge_fraction"] == pytest.approx(0.009693877551020426)
    assert intent.metadata["annualized_return"] == pytest.approx(2.830612244897964)


def test_kalshi_0995_one_deep_fill_signals() -> None:
    """0.995 ask, ALL 100 contracts fillable in ONE level.

    fee: `model_fee = 100 * 0.07 * 0.995 * 0.005 = 0.034825`, ceiled to
    6dp -> `0.034825` (already exact there), then ceiled to the nearest
    cent -> `$0.04` total for the ONE fill, i.e. `$0.0004`/contract.
    gas: `$0.0005`/contract (unchanged).
    residual: `1 - 0.995 - 0.0004 - 0.0005 = 0.0041`/contract.
    fraction: `0.0041 / 0.995 = 0.0041206030150753815` (0.4121%).
    annualized: `0.0041206030150753815 * (8760 / 30) = 1.2032160804020113`
    (120.32%) -- clears `min_viable_annualized` -> SIGNAL. The identical
    ask that produces `None` in the fragmented case below signals here,
    because a single fill pays Kalshi's $0.01-ish ceiling only ONCE
    across the whole 100-contract order, not a hundred times.
    """
    now = utcnow()
    snapshot = _kalshi_snapshot(
        yes_price=0.995,
        timestamp=now,
        end_date=now - timedelta(hours=1),
        book=_one_fill_book(price=0.995),
    )
    strategy = SettlementEdgeStrategy()

    intent = strategy.evaluate(
        snapshot,
        in_dispute_window=False,
        expected_resolution_ts=now + timedelta(hours=30),
    )

    assert intent is not None
    assert intent.metadata["fill_count"] == 1
    assert intent.metadata["fee"] == pytest.approx(0.0004)
    assert intent.metadata["edge"] == pytest.approx(0.004100000000000005)
    assert intent.metadata["net_edge_fraction"] == pytest.approx(0.0041206030150753815)
    assert intent.metadata["annualized_return"] == pytest.approx(1.2032160804020113)


def test_kalshi_0995_fragmented_book_fees_eat_the_residual_none() -> None:
    """Same 0.995 ask, but a hundred 1-contract levels -- fragmentation
    alone erases the residual.

    fee: `100 * ceil_cents(1 * 0.07 * 0.995 * 0.005) = 100 * $0.01 =
    $1.00` total, `$0.01`/contract -- the SAME per-fill $0.01 as 0.98's
    fragmented case above; Kalshi's floor does not care what the price
    was, only that each fill's `model_fee` rounds below one cent.
    gas: `$0.0005`/contract (unchanged).
    residual: `1 - 0.995 - 0.01 - 0.0005 = -0.0055`/contract (NEGATIVE)
    -- the fee ALONE (before gas) already exceeds the entire gross
    residual (`1 - 0.995 = 0.005`). "Fees eat it", literally, and it is
    a genuine consequence of a hundred small fills, not an artifact of
    pricing the fee at an assumed size that never matched the book.
    -> None.
    """
    now = utcnow()
    snapshot = _kalshi_snapshot(
        yes_price=0.995,
        timestamp=now,
        end_date=now - timedelta(hours=1),
        book=_fragmented_book(price=0.995),
    )
    strategy = SettlementEdgeStrategy()

    intent = strategy.evaluate(
        snapshot,
        in_dispute_window=False,
        expected_resolution_ts=now + timedelta(hours=30),
    )

    assert intent is None


def test_dispute_window_blocks_by_default() -> None:
    """An otherwise-signaling market is refused while `in_dispute_window`.

    Same 0.98/30h setup as the one-deep-fill test above -- would signal
    but for `in_dispute_window=True` -- and `allow_dispute_window`
    defaults `False` (PLAN.md R2): during the challenge/settlement-timer
    window, the "determined" outcome is precisely what is under contest.
    """
    now = utcnow()
    snapshot = _kalshi_snapshot(
        yes_price=0.98,
        timestamp=now,
        end_date=now - timedelta(hours=1),
        book=_one_fill_book(price=0.98),
    )
    strategy = SettlementEdgeStrategy()
    assert strategy.config["allow_dispute_window"] is False

    intent = strategy.evaluate(
        snapshot,
        in_dispute_window=True,
        expected_resolution_ts=now + timedelta(hours=30),
    )

    assert intent is None


def test_dispute_window_allowed_when_explicitly_configured() -> None:
    """The SAME dispute-window market signals once `allow_dispute_window=True`.

    Proves the block above is really `allow_dispute_window`'s doing (not
    some other rejection reason) -- flipping only that one config key,
    everything else identical to the blocked test above (including the
    one-deep-fill book), produces the same numbers the one-deep-fill
    0.98 test computed by hand: annualized_return = 5.393061224489802.
    """
    now = utcnow()
    snapshot = _kalshi_snapshot(
        yes_price=0.98,
        timestamp=now,
        end_date=now - timedelta(hours=1),
        book=_one_fill_book(price=0.98),
    )
    strategy = SettlementEdgeStrategy({"allow_dispute_window": True})

    intent = strategy.evaluate(
        snapshot,
        in_dispute_window=True,
        expected_resolution_ts=now + timedelta(hours=30),
    )

    assert intent is not None
    assert intent.metadata["annualized_return"] == pytest.approx(5.393061224489802)
    assert intent.metadata["in_dispute_window"] is True


def test_close_time_not_yet_passed_is_not_outcome_determined() -> None:
    """A 0.98 price BEFORE its scheduled close is not yet `outcome_determined`.

    PLAN.md D10(c) requires close_time to have passed, not merely an
    extreme price -- a market still trading normally at 0.98 with hours
    left on the clock is an ordinary favorite, not a settled outcome.
    """
    now = utcnow()
    snapshot = _kalshi_snapshot(
        yes_price=0.98, timestamp=now, end_date=now + timedelta(hours=5)
    )
    strategy = SettlementEdgeStrategy()

    intent = strategy.evaluate(
        snapshot, in_dispute_window=False, expected_resolution_ts=now + timedelta(hours=30)
    )

    assert intent is None


def test_price_not_at_either_tail_is_not_outcome_determined() -> None:
    """A merely favored (not near-certain) price never fires, close or not."""
    now = utcnow()
    snapshot = _kalshi_snapshot(
        yes_price=0.80, timestamp=now, end_date=now - timedelta(hours=1)
    )
    strategy = SettlementEdgeStrategy()

    intent = strategy.evaluate(
        snapshot, in_dispute_window=False, expected_resolution_ts=now + timedelta(hours=30)
    )

    assert intent is None


def test_on_market_data_delegates_with_in_dispute_window_assumed_false() -> None:
    """`on_market_data` (the generic/backtest entrypoint) reproduces
    `evaluate(..., in_dispute_window=False)` exactly when the snapshot
    itself supplies no better estimate of settlement time (it falls back
    to `end_date + settlement_delay_hours`): `end_date` is `now - 1h`
    here, so the estimate is `now - 1h + 24h = now + 23h`, i.e.
    `hours_to_resolution = 23`, not the `30` the explicit-estimate tests
    above used. No book is supplied either, so this also exercises the
    single-fill top-of-book fallback (`_priced_fills`'s optimistic end).
    """
    now = utcnow()
    snapshot = _kalshi_snapshot(
        yes_price=0.98, timestamp=now, end_date=now - timedelta(hours=1)
    )
    strategy = SettlementEdgeStrategy()

    intent = strategy.on_market_data(snapshot)

    assert intent is not None
    assert intent.metadata["fill_count"] == 1
    assert intent.metadata["hours_to_resolution"] == pytest.approx(23.0)
    assert intent.metadata["in_dispute_window"] is False


def test_ambiguity_and_clarity_keys_are_gone_from_default_config() -> None:
    """PLAN.md D10: the pre-T20 keyword heuristics and their config keys
    are deleted outright, not merely unused.
    """
    assert "ambiguity_keywords" not in DEFAULT_CONFIG
    assert "clarity_keywords" not in DEFAULT_CONFIG
    assert "clarity_confidence_boost" not in DEFAULT_CONFIG
