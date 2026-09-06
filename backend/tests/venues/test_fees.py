"""Tests for `app.venues.fees` (T05).

Derived from TASKS.md T05's acceptance lines, not from the implementation:
  1. Four worked examples (expected numbers computed by hand in comments,
     GUARDRAILS.md §5) pass to 1e-9, the Kalshi ceil case exact:
       - Polymarket taker, 100 contracts @ 0.60, rate 0.05:
         100 * 0.05 * 0.60 * 0.40 = 1.20
       - Polymarket maker (any rate): 0.0 (makers never pay, PLAN.md §3).
       - Kalshi taker, 100 contracts @ 0.50, rate 0.07:
         100 * 0.07 * 0.50 * 0.50 = 1.75
       - Kalshi taker, 1 contract @ 0.99, rate 0.07:
         1 * 0.07 * 0.99 * 0.01 = 0.000693 -> ceil_6dp(0.000693) = 0.000693
         (already exactly on a 6dp boundary, so the ceiling is a no-op).
  2. Fee is symmetric in price <-> 1 - price (the `p * (1 - p)` term is
     invariant under that swap).
  3. Fee is maximal at price = 0.5 over a grid of prices.

All fees are USD, `>= 0`, never a rate (GUARDRAILS.md §4).

T05 RETRY (this file's later sections): passed verify once, then
second-verifier/red-team found three real defects, closed here:
  DEFECT 1 — `fee()` didn't validate its own `price`/`size_contracts`
    arguments and could return a NEGATIVE fee (or NaN/inf, or crash with
    an uncaught `decimal.InvalidOperation`) for out-of-domain input —
    most realistically a stale integer-cents Kalshi payload
    (`price=50.0` instead of `0.50`).
  DEFECT 2 — `category_rate()`'s override-dict lookup broke on category
    casing: PLAN.md §3 spells categories capitalized (`Crypto`,
    `Geopolitics`), but only the incoming category was lowercased, so a
    `POLYMARKET_TAKER_FEE_OVERRIDES` override typed in that natural
    casing was silently discarded.
  DEFECT 3 — `POLYMARKET_CATEGORY_TAKER_RATES` was a mutable
    module-level `dict`; any importer could mutate it in place and
    change every subsequent fee process-wide.
"""
import math

import pytest

from app.config import Settings
from app.venues.fees import (
    POLYMARKET_CATEGORY_TAKER_RATES,
    KalshiFeeModel,
    PolymarketFeeModel,
    category_fee_schedule,
    category_rate,
    default_kalshi_schedule,
)
from app.venues.types import FeeSchedule

# ---------------------------------------------------------------------------
# Worked examples (the four required by TASKS.md acceptance line 1)
# ---------------------------------------------------------------------------


def test_polymarket_taker_worked_example() -> None:
    """100 contracts @ 0.60, rate 0.05: 100 * 0.05 * 0.60 * 0.40 = 1.20."""
    model = PolymarketFeeModel()
    schedule = FeeSchedule(taker_rate=0.05, maker_rate=0.0, source="category_table")

    fee = model.fee(price=0.60, size_contracts=100.0, liquidity="taker", schedule=schedule)

    assert abs(fee - 1.20) < 1e-9


def test_polymarket_maker_always_pays_zero() -> None:
    """Makers never pay on Polymarket (PLAN.md §3), regardless of rate."""
    model = PolymarketFeeModel()
    schedule = FeeSchedule(taker_rate=0.05, maker_rate=0.05, source="category_table")

    fee = model.fee(price=0.60, size_contracts=100.0, liquidity="maker", schedule=schedule)

    assert fee == 0.0


def test_kalshi_taker_worked_example() -> None:
    """100 contracts @ 0.50, rate 0.07: 100 * 0.07 * 0.50 * 0.50 = 1.75."""
    model = KalshiFeeModel()
    schedule = FeeSchedule(taker_rate=0.07, maker_rate=0.0, source="settings_default")

    fee = model.fee(price=0.50, size_contracts=100.0, liquidity="taker", schedule=schedule)

    assert abs(fee - 1.75) < 1e-9


def test_kalshi_ceil_6dp_worked_example_is_exact() -> None:
    """1 contract @ 0.99, rate 0.07: 1 * 0.07 * 0.99 * 0.01 = 0.000693.

    `0.000693` already sits exactly on a 6-decimal-place boundary, so
    `ceil_6dp` is a no-op here — this is the "Kalshi ceil case exact"
    acceptance line. `round_net_to_cents=False` isolates the 6dp
    ceiling from the separate whole-cent floor (see
    `test_kalshi_round_net_to_cents_floors_small_fees_to_a_cent` below).
    """
    model = KalshiFeeModel(round_net_to_cents=False)
    schedule = FeeSchedule(taker_rate=0.07, maker_rate=0.0, source="settings_default")

    fee = model.fee(price=0.99, size_contracts=1.0, liquidity="taker", schedule=schedule)

    assert abs(fee - 0.000693) < 1e-9


# ---------------------------------------------------------------------------
# Shape: symmetric in p <-> 1-p, maximal at p=0.5
# ---------------------------------------------------------------------------


def test_fee_is_symmetric_in_p_and_one_minus_p() -> None:
    """`p * (1 - p)` is invariant under swapping p and 1 - p.

    A near-resolution market (p close to 0 or 1) costs almost nothing to
    trade on either side; a coin-flip market (p near 0.5) costs the most
    on either side too. Checked at several (p, 1-p) pairs, not just 0.5.
    """
    model = PolymarketFeeModel()
    schedule = FeeSchedule(taker_rate=0.05, maker_rate=0.0, source="category_table")

    for p in (0.05, 0.2, 0.3, 0.45, 0.5):
        fee_p = model.fee(price=p, size_contracts=100.0, liquidity="taker", schedule=schedule)
        fee_q = model.fee(
            price=1.0 - p, size_contracts=100.0, liquidity="taker", schedule=schedule
        )
        assert abs(fee_p - fee_q) < 1e-9


def test_fee_is_maximal_at_p_half_over_a_grid() -> None:
    """The fee at price=0.5 is >= the fee at every other price on a grid.

    This is the asymmetry that matters for arbitrage edges: a flat
    percentage-of-notional fee would not have this shape at all, but
    `size * rate * p * (1 - p)` is maximized at p=0.5 and vanishes
    toward the tails.
    """
    model = PolymarketFeeModel()
    schedule = FeeSchedule(taker_rate=0.05, maker_rate=0.0, source="category_table")

    fee_at_half = model.fee(price=0.5, size_contracts=100.0, liquidity="taker", schedule=schedule)
    grid = [i / 100 for i in range(1, 100)]

    for p in grid:
        fee_p = model.fee(price=p, size_contracts=100.0, liquidity="taker", schedule=schedule)
        assert fee_p <= fee_at_half + 1e-9


# ---------------------------------------------------------------------------
# Kalshi ceiling / cents-floor mechanics
# ---------------------------------------------------------------------------


def test_kalshi_round_net_to_cents_defaults_true() -> None:
    """`round_net_to_cents` defaults to `True` (the non-direct-member floor)."""
    model = KalshiFeeModel()

    assert model.round_net_to_cents is True


def test_kalshi_round_net_to_cents_floors_small_fees_to_a_cent() -> None:
    """With the default `round_net_to_cents=True`, the ceil_6dp result
    (0.000693, from the worked example above) is further rounded UP to
    the nearest whole cent: ceil_cents(0.000693) = 0.01.
    """
    model = KalshiFeeModel()  # round_net_to_cents=True by default
    schedule = FeeSchedule(taker_rate=0.07, maker_rate=0.0, source="settings_default")

    fee = model.fee(price=0.99, size_contracts=1.0, liquidity="taker", schedule=schedule)

    assert abs(fee - 0.01) < 1e-9


def test_kalshi_ceil_is_per_fill_not_per_order() -> None:
    """Two separate fills each pay their own rounded-up ceiling.

    Two 1-contract fills at 0.99 (rate 0.07) each ceil to 0.01
    (round_net_to_cents=True), for a total of 0.02 across the order —
    NOT one ceiling applied to the order's combined 2-contract size
    (which would be 2 * 0.07 * 0.99 * 0.01 = 0.001386 -> ceil6dp
    0.001386 -> ceil_cents 0.01, a different and smaller number). This
    is why a caller must call `fee()` once per `Fill` and sum, never
    once with an order's aggregate size.
    """
    model = KalshiFeeModel()
    schedule = FeeSchedule(taker_rate=0.07, maker_rate=0.0, source="settings_default")

    per_fill = model.fee(price=0.99, size_contracts=1.0, liquidity="taker", schedule=schedule)
    total_two_fills = per_fill + per_fill
    combined_order_fee = model.fee(
        price=0.99, size_contracts=2.0, liquidity="taker", schedule=schedule
    )

    assert abs(total_two_fills - 0.02) < 1e-9
    assert total_two_fills > combined_order_fee


def test_kalshi_maker_uses_maker_rate() -> None:
    """A maker fill uses `schedule.maker_rate`, not `taker_rate` — some
    series carry a nonzero maker fee (PLAN.md §3), so this must not be
    hardcoded to 0 the way Polymarket's is.

    100 @ 0.50, maker_rate 0.03: 100 * 0.03 * 0.50 * 0.50 = 0.75.
    """
    model = KalshiFeeModel(round_net_to_cents=False)
    schedule = FeeSchedule(taker_rate=0.07, maker_rate=0.03, source="settings_default")

    maker_fee = model.fee(price=0.50, size_contracts=100.0, liquidity="maker", schedule=schedule)
    taker_fee = model.fee(price=0.50, size_contracts=100.0, liquidity="taker", schedule=schedule)

    assert abs(maker_fee - 0.75) < 1e-9
    assert maker_fee != taker_fee


def test_fee_never_negative() -> None:
    """Every fee returned by either model is >= 0, at the price extremes too."""
    poly = PolymarketFeeModel()
    kalshi = KalshiFeeModel()
    schedule = FeeSchedule(taker_rate=0.05, maker_rate=0.0, source="category_table")

    for price in (0.0, 1.0, 0.5):
        assert poly.fee(price=price, size_contracts=100.0, liquidity="taker", schedule=schedule) >= 0.0
        assert kalshi.fee(price=price, size_contracts=100.0, liquidity="taker", schedule=schedule) >= 0.0


# ---------------------------------------------------------------------------
# `category_rate` normalizer
# ---------------------------------------------------------------------------


def test_category_rate_is_case_insensitive() -> None:
    """`"Crypto"`, `"CRYPTO"`, and `"crypto"` all resolve to the same rate."""
    assert category_rate("Crypto") == category_rate("CRYPTO") == category_rate("crypto")
    assert category_rate("crypto") == POLYMARKET_CATEGORY_TAKER_RATES["crypto"]


def test_category_rate_geopolitics_is_zero() -> None:
    """`"geopolitics"` (any case) maps to 0.0 (PLAN.md §3)."""
    assert category_rate("geopolitics") == 0.0
    assert category_rate("Geopolitics") == 0.0


def test_category_rate_unknown_defaults_to_point_zero_five() -> None:
    """An unrecognized category, and `None`, both fall back to 0.05."""
    assert category_rate("some-made-up-category") == 0.05
    assert category_rate(None) == 0.05


def test_category_rate_known_categories_match_plan() -> None:
    """Spot-check the category table against PLAN.md §3's four tiers."""
    assert category_rate("crypto") == 0.07
    assert category_rate("sports") == 0.05
    assert category_rate("politics") == 0.04
    assert category_rate("geopolitics") == 0.0


# ---------------------------------------------------------------------------
# Settings-sourced defaults
# ---------------------------------------------------------------------------


def test_default_kalshi_schedule_reads_settings() -> None:
    """`default_kalshi_schedule()` sources its rates from `Settings`, not
    a literal in this module's public API (GUARDRAILS.md §1.5).
    """
    from app.config import settings

    schedule = default_kalshi_schedule()

    assert schedule.taker_rate == settings.kalshi_taker_fee_rate
    assert schedule.maker_rate == settings.kalshi_maker_fee_rate
    assert schedule.source == "settings_default"


# ---------------------------------------------------------------------------
# DEFECT 1 (retry): `fee()` validates its own `price`/`size_contracts`
# ---------------------------------------------------------------------------
#
# Every case below previously returned a negative number, NaN, +/-inf, or
# raised an uncaught `decimal.InvalidOperation` instead of a `ValueError`.
# GUARDRAILS.md §1.5 / the `FeeModel` ABC docstring both state the return
# is always `>= 0`; a negative "fee" is a credit an arbitrage scorer would
# read as free money.

_PM_SCHEDULE = FeeSchedule(taker_rate=0.05, maker_rate=0.0, source="category_table")
_KS_SCHEDULE_07 = FeeSchedule(taker_rate=0.07, maker_rate=0.0, source="settings_default")


def test_polymarket_fee_rejects_price_above_one() -> None:
    """`price=1.5` (out of `[0,1]`) raises, rather than returning -3.75."""
    model = PolymarketFeeModel()
    with pytest.raises(ValueError, match="price"):
        model.fee(price=1.5, size_contracts=100.0, liquidity="taker", schedule=_PM_SCHEDULE)


def test_polymarket_fee_rejects_negative_price() -> None:
    """`price=-0.5` raises, rather than returning -3.75."""
    model = PolymarketFeeModel()
    with pytest.raises(ValueError, match="price"):
        model.fee(price=-0.5, size_contracts=100.0, liquidity="taker", schedule=_PM_SCHEDULE)


def test_kalshi_fee_rejects_negative_size() -> None:
    """`size_contracts=-100` raises, rather than returning -1.25."""
    model = KalshiFeeModel()
    with pytest.raises(ValueError, match="size_contracts"):
        model.fee(price=0.5, size_contracts=-100.0, liquidity="taker", schedule=_KS_SCHEDULE_07)


def test_kalshi_fee_rejects_cents_mistake_price() -> None:
    """A stale integer-cents payload passing `price=50.0` (PLAN.md §3's
    realistic trigger: a forgotten `/100`) must raise, not silently
    compute a -$17,150 "fee" for a real $1.75 fill.
    """
    model = KalshiFeeModel()
    with pytest.raises(ValueError, match="price"):
        model.fee(price=50.0, size_contracts=100.0, liquidity="taker", schedule=_KS_SCHEDULE_07)


@pytest.mark.parametrize("model_cls,schedule", [(PolymarketFeeModel, _PM_SCHEDULE), (KalshiFeeModel, _KS_SCHEDULE_07)])
def test_fee_rejects_nan_price(model_cls: type, schedule: FeeSchedule) -> None:
    """A NaN `price` raises for both models, rather than propagating NaN."""
    model = model_cls()
    with pytest.raises(ValueError, match="price"):
        model.fee(price=math.nan, size_contracts=100.0, liquidity="taker", schedule=schedule)


@pytest.mark.parametrize("model_cls,schedule", [(PolymarketFeeModel, _PM_SCHEDULE), (KalshiFeeModel, _KS_SCHEDULE_07)])
def test_fee_rejects_nan_size(model_cls: type, schedule: FeeSchedule) -> None:
    """A NaN `size_contracts` raises for both models, rather than propagating NaN."""
    model = model_cls()
    with pytest.raises(ValueError, match="size_contracts"):
        model.fee(price=0.5, size_contracts=math.nan, liquidity="taker", schedule=schedule)


def test_polymarket_fee_rejects_infinite_price() -> None:
    """`price=inf` raises for Polymarket, rather than returning -inf."""
    model = PolymarketFeeModel()
    with pytest.raises(ValueError, match="price"):
        model.fee(price=math.inf, size_contracts=100.0, liquidity="taker", schedule=_PM_SCHEDULE)


def test_kalshi_fee_rejects_infinite_price() -> None:
    """`price=inf` raises a `ValueError` for Kalshi, not an uncaught
    `decimal.InvalidOperation`.
    """
    model = KalshiFeeModel()
    with pytest.raises(ValueError, match="price"):
        model.fee(price=math.inf, size_contracts=100.0, liquidity="taker", schedule=_KS_SCHEDULE_07)


def test_kalshi_fee_rejects_absurdly_large_size_instead_of_crashing() -> None:
    """A huge but technically finite `size_contracts` (e.g. `1e300`) must
    raise `ValueError`, not an uncaught `decimal.InvalidOperation` from
    `Decimal.quantize` overflowing its context precision.
    """
    model = KalshiFeeModel()
    with pytest.raises(ValueError):
        model.fee(price=0.5, size_contracts=1e300, liquidity="taker", schedule=_KS_SCHEDULE_07)


def test_polymarket_fee_huge_size_stays_finite_and_nonnegative() -> None:
    """Polymarket's formula doesn't go through `Decimal`, so a huge
    `size_contracts` still returns a large-but-finite, non-negative fee
    rather than crashing — it is not itself out of `_check_size`'s
    domain (finite, >= 0), just an unrealistic input.
    """
    model = PolymarketFeeModel()
    fee = model.fee(price=0.5, size_contracts=1e300, liquidity="taker", schedule=_PM_SCHEDULE)
    assert math.isfinite(fee)
    assert fee >= 0.0


# ---------------------------------------------------------------------------
# DEFECT 2 (retry): override casing
# ---------------------------------------------------------------------------


def test_category_rate_override_takes_effect_regardless_of_typed_casing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator override in `Crypto`/`GEOPOLITICS`/`geopolitics` casing
    must all take effect — PLAN.md §3 spells categories capitalized
    (`Crypto`, `Geopolitics`), which is exactly what an operator is
    likely to type into `POLYMARKET_TAKER_FEE_OVERRIDES`.
    """
    import app.venues.fees as fees_module

    overridden = Settings(
        POLYMARKET_TAKER_FEE_OVERRIDES={
            "Crypto": 0.02,
            "GEOPOLITICS": 0.03,
            "geopolitics": 0.03,
        }
    )
    monkeypatch.setattr(fees_module, "settings", overridden)

    assert category_rate("Crypto") == 0.02
    assert category_rate("crypto") == 0.02
    assert category_rate("CRYPTO") == 0.02
    assert category_rate("Geopolitics") == 0.03
    assert category_rate("geopolitics") == 0.03


def test_category_rate_explicit_zero_override_is_honored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit `0.0` override must still be honored (not confused
    with "no override"): `category_rate` uses `in`, not truthiness, to
    check for an override — this pins that down against regression.
    """
    import app.venues.fees as fees_module

    overridden = Settings(POLYMARKET_TAKER_FEE_OVERRIDES={"Crypto": 0.0})
    monkeypatch.setattr(fees_module, "settings", overridden)

    # The category table default for crypto is 0.07 (nonzero) -- if the
    # explicit 0.0 override were dropped in favor of the table default,
    # this would wrongly come back nonzero.
    assert POLYMARKET_CATEGORY_TAKER_RATES["crypto"] == 0.07
    assert category_rate("Crypto") == 0.0


# ---------------------------------------------------------------------------
# Phase-1 remediation FIX 3: `category_fee_schedule()` labels an operator
# override's provenance so it is never mistaken for the published table's
# genuine zero.
# ---------------------------------------------------------------------------


def test_category_fee_schedule_no_override_is_category_table() -> None:
    """With no override, `category_fee_schedule` matches `category_rate`
    exactly and keeps the DECLARED `"category_table"` source.
    """
    schedule = category_fee_schedule("crypto")
    assert schedule.taker_rate == category_rate("crypto") == 0.07
    assert schedule.maker_rate == 0.0
    assert schedule.source == "category_table"


def test_category_fee_schedule_geopolitics_is_still_category_table() -> None:
    """The published table's OWN genuine zero (Geopolitics) is unaffected
    — it is still a DECLARED source, exactly as before this fix.
    """
    schedule = category_fee_schedule("geopolitics")
    assert schedule.taker_rate == 0.0
    assert schedule.source == "category_table"


def test_category_fee_schedule_operator_override_is_settings_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator override — even a NONZERO one — is labeled
    `"settings_override"`, distinct from the published table's
    `"category_table"`, so its provenance is always traceable.
    """
    import app.venues.fees as fees_module

    overridden = Settings(POLYMARKET_TAKER_FEE_OVERRIDES={"Crypto": 0.08})
    monkeypatch.setattr(fees_module, "settings", overridden)

    schedule = category_fee_schedule("crypto")
    assert schedule.taker_rate == 0.08
    assert schedule.source == "settings_override"


def test_category_fee_schedule_operator_zeroed_rate_is_not_category_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect this fix closes: `POLYMARKET_TAKER_FEE_OVERRIDES=
    {"politics": 0.0}` must NOT wear the `"category_table"` label — that
    label is `SimulatedFillEngine`'s TRUSTED-zero source, and an operator
    override is exactly the kind of source that guard exists to catch.
    """
    import app.venues.fees as fees_module

    overridden = Settings(POLYMARKET_TAKER_FEE_OVERRIDES={"politics": 0.0})
    monkeypatch.setattr(fees_module, "settings", overridden)

    schedule = category_fee_schedule("politics")
    assert schedule.taker_rate == 0.0
    assert schedule.source == "settings_override"
    assert schedule.source != "category_table"


def test_category_fee_schedule_none_category_is_category_table() -> None:
    """`category=None` can never be an override hit (no key to match)."""
    schedule = category_fee_schedule(None)
    assert schedule.taker_rate == category_rate(None)
    assert schedule.source == "category_table"


# ---------------------------------------------------------------------------
# DEFECT 3 (retry): the category table cannot be mutated
# ---------------------------------------------------------------------------


def test_category_table_cannot_be_mutated() -> None:
    """`POLYMARKET_CATEGORY_TAKER_RATES` is a `MappingProxyType`: an
    importer attempting `POLYMARKET_CATEGORY_TAKER_RATES["crypto"] = 0.0`
    must raise `TypeError`, not silently change every subsequent fee
    process-wide.
    """
    with pytest.raises(TypeError):
        POLYMARKET_CATEGORY_TAKER_RATES["crypto"] = 0.0  # type: ignore[index]

    # Unaffected by the failed mutation attempt above.
    assert POLYMARKET_CATEGORY_TAKER_RATES["crypto"] == 0.07
