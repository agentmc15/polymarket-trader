"""Concrete `FeeModel`s (PLAN.md D3, T05).

Both venues share the same underlying formula shape —
``fee = size_contracts * rate * price * (1 - price)`` — sourced from
PLAN.md §3 "Venue facts" (vendor docs pinned by the architect on
2026-09-04; GUARDRAILS.md §1.4 forbids re-fetching them here). The
``price * (1 - price)`` term means the fee is MAXIMAL at ``price=0.5``
(a coin-flip market) and vanishes toward the tails (``price`` near 0.0
or 1.0, i.e. a near-resolution market) — the opposite of a flat
percentage-of-notional fee. This asymmetry is why every fee returned
here is symmetric in ``price`` vs ``1 - price`` and maximized at
``price=0.5`` (see ``tests/venues/test_fees.py``): it drives which
arbitrage edges actually survive after costs.

Both `FeeSchedule` (rate + source) and the `FeeModel` ABC already exist —
`FeeSchedule` in `app/venues/types.py`, `FeeModel` in `app/venues/base.py`
(both placed there by T04 so `VenueMarket.fee`/`VenueAdapter.fee_model()`
have concrete types before this module existed). This module imports and
subclasses both; it does NOT redefine either.

Units (GUARDRAILS.md §4): ``price`` is a probability in ``[0.0, 1.0]``;
``size_contracts`` is contracts (each pays $1.00 at resolution); every
fee returned is USD, always ``>= 0``, never a rate.

GUARDRAILS.md §1.5: fees are never literals in strategy code. Strategies
must obtain a `FeeSchedule` (from a venue payload, `category_rate()`
below, or `Settings`) and call `PolymarketFeeModel.fee()` /
`KalshiFeeModel.fee()` — never hardcode a rate.
"""
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from types import MappingProxyType

from app.config import settings
from app.venues.base import FeeModel
from app.venues.types import FeeSchedule, Liquidity, _check_price, _check_size

#: Polymarket per-category taker fee rates (PLAN.md §3 Venue facts,
#: docs.polymarket.com/trading/fees, fetched by the architect 2026-09-04).
#: Dimensionless fee rates, keyed by lowercased category name. The
#: per-market rate carried on a CLOB market payload (historically
#: `maker_base_fee`/`taker_base_fee`) is authoritative over this table
#: when present (PLAN.md §3) — callers that have such a payload rate
#: should build a `FeeSchedule` directly from it (`source="clob_market"`)
#: rather than going through `category_rate()`.
#:
#: Wrapped in `MappingProxyType` (DEFECT 3) so an importer cannot mutate
#: it in place (e.g. `POLYMARKET_CATEGORY_TAKER_RATES["crypto"] = 0.0`)
#: and silently change every subsequent fee process-wide — the same
#: guard `app/venues/types.py` already applies to every comparable
#: collection it exposes. The public name is unchanged; only real
#: `dict` mutation is now blocked, not read access.
POLYMARKET_CATEGORY_TAKER_RATES: MappingProxyType[str, float] = MappingProxyType(
    {
        "crypto": 0.07,
        "sports": 0.05,
        "economics": 0.05,
        "culture": 0.05,
        "weather": 0.05,
        "other": 0.05,
        "finance": 0.04,
        "politics": 0.04,
        "mentions": 0.04,
        "tech": 0.04,
        "geopolitics": 0.0,
    }
)

#: Fallback taker rate for a category not present in
#: `POLYMARKET_CATEGORY_TAKER_RATES` (and not `"geopolitics"`, which is
#: explicitly 0.0 above). PLAN.md §3 enumerates the known categories;
#: an unrecognized one is treated as the more conservative "Sports/
#: Economics/Culture/Weather/Other" tier (0.05) rather than the lowest
#: known rate, so an unmodeled category never understates its cost.
_POLYMARKET_UNKNOWN_CATEGORY_RATE = 0.05


def category_rate(category: str | None) -> float:
    """Normalize a Polymarket category name to its taker fee rate.

    Args:
        category: Venue-supplied category name, e.g. `"Crypto"`,
            `"Politics"`. Matching is case-insensitive. `None` or an
            unrecognized category maps to
            `_POLYMARKET_UNKNOWN_CATEGORY_RATE` (0.05) — EXCEPT that a
            category is never silently invented as `"geopolitics"`;
            that name must be spelled out to get the 0.0 rate.

    Returns:
        float: Dimensionless taker fee rate, `>= 0`. Checked first
            against `settings.polymarket_taker_fee_overrides` (an
            operator-configurable override keyed the same way), then
            `POLYMARKET_CATEGORY_TAKER_RATES`, then the unknown-category
            fallback.
    """
    if category is None:
        return _POLYMARKET_UNKNOWN_CATEGORY_RATE
    key = category.strip().lower()
    if key in settings.polymarket_taker_fee_overrides:
        return settings.polymarket_taker_fee_overrides[key]
    return POLYMARKET_CATEGORY_TAKER_RATES.get(key, _POLYMARKET_UNKNOWN_CATEGORY_RATE)


def category_fee_schedule(category: str | None) -> FeeSchedule:
    """Build the `FeeSchedule` a Polymarket taker fill should be priced with.

    Phase-1 remediation FIX 3. `category_rate()` above returns a bare
    rate, and when `settings.polymarket_taker_fee_overrides` supplies
    one it returns exactly whatever the operator typed — including
    `0.0`. Every call site that used to build
    `FeeSchedule(taker_rate=category_rate(category), maker_rate=0.0,
    source="category_table")` therefore stamped an operator override
    with the same `source` the PUBLISHED category table uses, which is
    one of `app.execution.fill_engine._ZERO_RATE_DECLARED_SOURCES` — the
    set of sources trusted to assert a genuinely-zero fee. That made
    `POLYMARKET_TAKER_FEE_OVERRIDES={"politics": 0.0}` produce a fill
    that is free because an operator typed a number, wearing the same
    label as a fill that is free because Polymarket's own published
    table says so (Geopolitics), so the fill engine's undeclared-zero-
    fee guard could never catch it. Kalshi's equivalent
    (`default_kalshi_schedule`) already stamps its `Settings`-sourced
    rate `"settings_default"`, which is NOT in the trusted set, so an
    operator-zeroed Kalshi rate is already flagged; this makes
    Polymarket match.

    Args:
        category: Venue-supplied category name, same normalization as
            `category_rate()`.

    Returns:
        FeeSchedule: `taker_rate=category_rate(category)`,
            `maker_rate=0.0`, and `source="settings_override"` (NOT
            trusted to assert a zero) when
            `settings.polymarket_taker_fee_overrides` supplied the
            rate, else `source="category_table"` (the published,
            DECLARED-zero source, unchanged from before this fix).
    """
    overridden = (
        category is not None
        and category.strip().lower() in settings.polymarket_taker_fee_overrides
    )
    return FeeSchedule(
        taker_rate=category_rate(category),
        maker_rate=0.0,
        source="settings_override" if overridden else "category_table",
    )


class PolymarketFeeModel(FeeModel):
    """Polymarket's taker-only CLOB fee (PLAN.md §3, T04's `FeeModel` ABC).

    Source: https://docs.polymarket.com/trading/fees (fetched by the
    architect 2026-09-04; PLAN.md §3 pins the values — GUARDRAILS.md
    §1.4 forbids re-fetching this page from task code).

    Formula: `fee = size_contracts * rate * price * (1 - price)`,
    charged in USDC and modeled here as USD. **Makers pay 0 regardless
    of the schedule's `maker_rate`** — Polymarket's CLOB is taker-only
    for fees (PLAN.md §3: "takers only, makers never pay"); a nonzero
    `schedule.maker_rate` is never applied by this model.

    Category default rates live in `POLYMARKET_CATEGORY_TAKER_RATES`
    above; `category_rate()` normalizes a venue category string to one.
    When a market's CLOB payload carries its own `maker_base_fee`/
    `taker_base_fee`, that rate is authoritative over the category table
    (PLAN.md §3) — build the `FeeSchedule` passed to `fee()` from that
    payload rate directly (`source="clob_market"`) rather than from
    `category_rate()`.
    """

    def fee(
        self,
        price: float,
        size_contracts: float,
        liquidity: Liquidity,
        schedule: FeeSchedule,
    ) -> float:
        """Return the dollar fee for one Polymarket fill.

        Args:
            price: Fill price, a probability in [0.0, 1.0].
            size_contracts: Fill size in contracts, `>= 0`.
            liquidity: `"maker"` or `"taker"`. `"maker"` always returns
                0.0 (see class docstring).
            schedule: The market's `FeeSchedule`; `schedule.taker_rate`
                is used for a taker fill.

        Returns:
            float: Fee in USD, `>= 0`.

        Raises:
            ValueError: If `price` is not a finite probability in
                `[0.0, 1.0]`, or `size_contracts` is not finite and
                `>= 0` (DEFECT 1, PLAN.md §3: an unvalidated `price`/
                `size_contracts` — e.g. a stale integer-cents payload
                passing `price=50.0` — can otherwise produce a negative,
                NaN, or infinite "fee", which an arbitrage scorer would
                read as free money).
        """
        _check_price(price, field="price")
        _check_size(size_contracts, field="size_contracts")
        if liquidity == "maker":
            return 0.0
        return size_contracts * schedule.taker_rate * price * (1.0 - price)


def _ceil_decimal(value: float, places: int) -> float:
    """Round `value` UP to `places` decimal digits, exactly.

    Used by `KalshiFeeModel.fee()` for both its 6-decimal-place ceiling
    and its optional whole-cent ceiling. Two naive approaches both fail
    here because a binary `float` frequently cannot represent a decimal
    value like `0.000693` or `1.75` exactly, so `value` often already
    carries a tiny (~1e-16 relative) representation-error excess (e.g.
    `1.7500000000000002` instead of `1.75`):

    - `math.ceil(value * 10**places) / 10**places` multiplies that
      excess up to the target scale before ceiling, turning a value
      that is conceptually already exactly on a boundary into one that
      appears to be just past it — `1.75` would wrongly ceil to
      `1.750001`.
    - Converting straight through `Decimal(str(value))` does NOT avoid
      this: `str()` on a float that already IS `1.7500000000000002`
      (as a bit pattern, not merely a display artifact) faithfully
      reports every one of those digits, so the same wrong ceiling
      happens one step later, inside `Decimal.quantize`.

    The fix is to round `value` to a much finer precision than `places`
    FIRST (`round(value, 12)`, six orders of magnitude past the 6dp
    Kalshi target and comfortably past the ~1e-16 relative noise for
    any realistic USD fee magnitude), which snaps it back to the
    nearest value at that finer precision and discards the noise
    without touching any digit that matters at `places`. Only then is
    it converted to `Decimal` (via `str()`, so the now-clean digits are
    used exactly rather than the finer float's own binary expansion)
    and ceiling-quantized to `places`.

    HARDENING 4 trade-off (adjudicated, low priority): the pre-round
    necessarily discards any GENUINE excess sitting within its own
    precision above a `places`-boundary, which would undercharge by one
    tick at `places`. A 300,000-combination sweep over the rates the two
    real venues actually publish (0.04, 0.05, 0.07), cent-tick prices,
    and whole-contract sizes found zero undercharges at 9dp — the
    reachable repro needs a rate with many more significant digits than
    any published rate, e.g. `0.04053802336689357`, which only an
    operator-typed `polymarket_taker_fee_overrides` value could supply
    (`app/config.py`'s `polymarket_taker_fee_overrides` accepts an
    arbitrary float; the category table and `Settings`' Kalshi rate
    fields do not). 12dp shrinks that already-narrow discard window by
    three more orders of magnitude while staying far above float64's
    ~1e-16 chained-subtraction noise floor, without moving to a full
    Decimal reimplementation of this hot loop (T07 territory).

    A `value` that is finite and `>= 0` (DEFECT 1's `_check_size` already
    guarantees that much on `KalshiFeeModel.fee()`'s inputs — but a huge
    yet still-finite `size_contracts`, e.g. `1e300`, is not itself out of
    that domain) can still be too large in MAGNITUDE for this: quantizing
    it to `places` decimal digits under `Decimal`'s default 28-significant-
    digit context raises `decimal.InvalidOperation` ("result too large
    for current context"). That is a numerically distinct failure from
    the ones `_check_price`/`_check_size` guard against — no realistic
    `[0,1]`-bounded `price` combined with a merely huge `size_contracts`
    produces a `math.isfinite`-failing float here, only a `Decimal`
    context overflow — so it is caught here, at the one place it can
    actually occur, and turned into the same `ValueError` contract as
    every other out-of-domain input.

    Args:
        value: The value to round up, `>= 0`.
        places: Number of decimal digits to round to.

    Returns:
        float: `value` rounded up to `places` decimal digits.

    Raises:
        ValueError: If `value`'s magnitude is too large to be
            represented exactly at `places` decimal digits.
    """
    quantum = Decimal(1).scaleb(-places)
    try:
        denoised = Decimal(str(round(value, 12)))
        return float(denoised.quantize(quantum, rounding=ROUND_CEILING))
    except InvalidOperation as exc:
        raise ValueError(
            f"value {value!r} is too large to ceiling-round to {places} decimal places"
        ) from exc


class KalshiFeeModel(FeeModel):
    """Kalshi's retail fee formula, rounded per fill (PLAN.md §3).

    NOT re-confirmed on 2026-09-04; 0.07 is Kalshi's historically
    published standard taker rate.

    Formula: `model_fee = size_contracts * rate * price * (1 - price)`,
    then `trade_fee = ceil_6dp(model_fee)` — the fee is rounded UP to
    the nearest $0.000001 (6 decimal places) — **per fill, not per
    order**: an order that fills across several price levels or in
    several partial fills pays this ceiling once per fill, so a
    multi-fill order's total fee is the sum of several independently
    rounded-up amounts, not one ceiling applied to the order's blended
    average. Callers that need an order-level total must call `fee()`
    once per `Fill` and sum the results, never call it once with an
    order's aggregate size.

    After the per-fill ceiling, when `round_net_to_cents` is `True`
    (the default — modeling the $0.01 floor applied to a fill's net for
    non-direct members, PLAN.md §3), the returned fee is additionally
    rounded UP to the nearest whole cent. Direct members are exempt from
    that floor; pass `round_net_to_cents=False` to model that case.

    `taker_rate`/`maker_rate` defaults come from `Settings`
    (`settings.kalshi_taker_fee_rate` / `settings.kalshi_maker_fee_rate`,
    0.07 / 0.0), never a literal in strategy code (GUARDRAILS.md §1.5);
    the module-level fallback below exists only so this class has a
    documented value if constructed with no arguments at all.
    """

    #: Documented fallback only — real callers should source a
    #: `KalshiFeeModel` from `Settings` (see class docstring), not rely
    #: on this default.
    _DEFAULT_ROUND_NET_TO_CENTS = True

    def __init__(self, round_net_to_cents: bool = _DEFAULT_ROUND_NET_TO_CENTS) -> None:
        """Configure whether the non-direct-member $0.01 net floor applies.

        Args:
            round_net_to_cents: If `True` (default), round the per-fill
                fee up to the nearest whole cent after the 6-decimal-
                place ceiling, modeling the non-direct-member floor.
                Pass `False` for a direct-member account, which is
                exempt from that floor.
        """
        self.round_net_to_cents = round_net_to_cents

    def fee(
        self,
        price: float,
        size_contracts: float,
        liquidity: Liquidity,
        schedule: FeeSchedule,
    ) -> float:
        """Return the dollar fee for one Kalshi fill.

        Args:
            price: Fill price, a probability in [0.0, 1.0].
            size_contracts: Fill size in contracts, `>= 0`.
            liquidity: `"maker"` or `"taker"` — selects
                `schedule.maker_rate`/`schedule.taker_rate`.
            schedule: The market's `FeeSchedule`.

        Returns:
            float: Fee in USD, `>= 0`, ceiled to 6 decimal places per
                fill and then, if `self.round_net_to_cents`, further
                rounded up to the nearest whole cent.

        Raises:
            ValueError: If `price` is not a finite probability in
                `[0.0, 1.0]`, or `size_contracts` is not finite and
                `>= 0` (DEFECT 1, PLAN.md §3: unvalidated out-of-domain
                input here can otherwise reach `Decimal` arithmetic in
                `_ceil_decimal` and raise an uncaught
                `decimal.InvalidOperation`, or silently produce a
                negative/NaN/infinite fee).
        """
        _check_price(price, field="price")
        _check_size(size_contracts, field="size_contracts")
        rate = schedule.maker_rate if liquidity == "maker" else schedule.taker_rate
        model_fee = size_contracts * rate * price * (1.0 - price)
        trade_fee = _ceil_decimal(model_fee, 6)
        if self.round_net_to_cents:
            trade_fee = _ceil_decimal(trade_fee, 2)
        return trade_fee


def default_kalshi_schedule(source: str = "settings_default") -> FeeSchedule:
    """Build a `FeeSchedule` from `Settings`' Kalshi defaults.

    Convenience for callers (T07/T10) that have no per-market Kalshi
    fee-waiver payload to read a rate from and just want the
    `Settings`-sourced standard rate (GUARDRAILS.md §1.5: never a
    literal in strategy code).

    Args:
        source: Recorded on the returned `FeeSchedule.source`. Default
            `"settings_default"`.

    Returns:
        FeeSchedule: `taker_rate=settings.kalshi_taker_fee_rate`,
            `maker_rate=settings.kalshi_maker_fee_rate`.
    """
    return FeeSchedule(
        taker_rate=settings.kalshi_taker_fee_rate,
        maker_rate=settings.kalshi_maker_fee_rate,
        source=source,
    )
