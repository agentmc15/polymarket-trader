"""Binary complement arbitrage strategy.

Exploits mispricing when a binary market's YES and NO asks sum to less
than 1.0 by more than costs, by buying BOTH outcomes and holding to
resolution: exactly one side always redeems for $1.00 per contract, so
the edge computed here is realized regardless of which way the market
resolves (PLAN.md D8 — this is the riskless, hold-to-resolution form,
not a buy/sell round trip).

GUARDRAILS.md §1.5: fees are never literals here. Every fee comes from
`app.venues.fees.PolymarketFeeModel`/`KalshiFeeModel`, fed a
`FeeSchedule` built from `snapshot.category` (Polymarket, via
`category_fee_schedule`, which itself honors
`settings.polymarket_taker_fee_overrides` and labels the schedule's
`source` accordingly) or from `Settings` (Kalshi, via
`default_kalshi_schedule`).

**Edge formula and how the fixed redemption gas enters it** (see
`_edge` for the exact arithmetic): per PLAN.md §3, Polymarket's taker
fee is `size_contracts * rate * price * (1 - price)` — a PER-CONTRACT
cost that scales linearly with size — while `settings.redemption_gas_usd`
is a Polygon gas cost charged PER POSITION regardless of size (one
redemption per outcome held, not per contract). Folding that fixed cost
into a per-contract edge requires an assumed contract count, and the
only size known at signal time (before `calculate_position_size`/the
engine's own cash-scaling runs) is `config["min_position_size"]` — the
same count this strategy already uses to walk `snapshot.book` for the
ask it prices legs at. This module amortizes the fixed
`2 * redemption_gas_usd` (one redemption per leg) over exactly that
count, so the edge estimate stays internally consistent with "the price
you'd actually get for `min_position_size` contracts" and is
CONSERVATIVE relative to any larger real fill (gas amortizes better,
i.e. costs less per contract, at any size above `min_position_size`).
The alternative (gate gas as a separate all-or-nothing viability check
rather than folding it into the per-contract threshold) was rejected
because `min_profit_margin` is the only threshold this strategy exposes
and a second, independent gas gate would let a config change one knob
without the other seeing it.

**Why `DEFAULT_CONFIG["min_position_size"]` is 100, not 10** (T10
retry defect fix — GUARDRAILS.md): the backtest/paper engine rejects
any intent whose total notional (`size_contracts * sum of leg limit
prices`, here `min_position_size * (yes_ask + no_ask)`) is below
`settings.min_trade_usd` (default $10 — see
`app.services.backtesting.engine._leg_sizes`). This strategy only
signals when `yes_ask + no_ask` is LOW (that is the edge), so a bigger
edge means a SMALLER notional per contract, not a larger one — sizing
the default against a high-price example (like the 0.93 below) and
shipping it is exactly how a previous default (`min_position_size =
10`) could clear the floor only at prices where the strategy never
signals, and never clear it at any price where it does: `10 * 0.93 =
9.30 < 10`, and every cheaper (more mispriced) `yes_ask + no_ask` makes
that worse, not better. At `min_position_size = 100`, `100 * 0.93 =
93` clears the floor with room to spare, and it still clears it down
to `yes_ask + no_ask ~= 0.10` (a 90% gross edge — already far outside
any plausible real mispricing) at exactly the $10 floor. `100` is also
where the fixed `2 * redemption_gas_usd` cost is actually amortized
rather than merely legal: at a 7% gross edge (asks summing to 0.93),
the net edge per contract is `-$0.0549` at 1 contract (gas alone
exceeds the gross edge), crosses to positive only around 2-3
contracts, and only asymptotes near the fee-only edge (`0.045128`/
contract) at real scale — `0.044128` at 100 contracts (2.2% of the
asymptote given up to gas) versus `0.045118` at 10,000 (an
unrealistically large single fill for a `min_liquidity` default of
$1,000). 100 is the smallest round size where the gas drag is already
close to negligible without assuming a fill size the strategy has no
basis to expect.

Example:
    YES ask = 0.45, NO ask = 0.48 -> gross edge = 1 - 0.93 = 0.07
    Category taker rate 0.05 -> fees = 0.05*0.45*0.55 + 0.05*0.48*0.52
        = 0.024855 per contract
    min_position_size = 100 (the shipped default) -> gas = 2*0.05 / 100
        = 0.001 per contract
    edge = 0.07 - 0.024855 - 0.001 = 0.044145 (4.41%) -> signal, since
    that clears the default `min_profit_margin` of 0.02. Notional at
    this size is `100 * 0.93 = $93`, clear of `settings.min_trade_usd`
    ($10).
"""
from typing import Any

from app.config import settings
from app.strategies.base import BaseStrategy, Intent, Leg, MarketSnapshot, Signal
from app.venues.base import FeeModel
from app.venues.fees import (
    KalshiFeeModel,
    PolymarketFeeModel,
    category_fee_schedule,
    default_kalshi_schedule,
)
from app.venues.types import FeeSchedule, VenueId

DEFAULT_CONFIG: dict[str, Any] = {
    # Minimum edge, PER CONTRACT and net of per-contract taker fees and
    # amortized redemption gas (see module docstring), required to signal.
    "min_profit_margin": 0.02,
    # Maximum notional for the intent as a fraction of portfolio equity
    # (enforced by the backtest engine's own leg sizing, not here).
    "max_position_pct": 0.10,
    # CONTRACTS, not dollars (unlike the pre-T10 config). Used as: (a)
    # the depth `on_market_data` walks `snapshot.book` for to find each
    # leg's sizing ask (see `_sizing_ask` — NOT top-of-book once a real
    # book is present); (b) the divisor that turns the fixed per-position
    # redemption gas into a per-contract cost (see module docstring);
    # and (c) the contract count both legs are given at signal time, so
    # a complement's two legs are always sized EQUAL IN CONTRACTS, never
    # equal in dollars (equal dollars of unequal-priced YES/NO would
    # leave a naked directional residual on the cheaper side).
    #
    # 100, NOT 10 (T10 retry defect fix — see module docstring's "Why
    # DEFAULT_CONFIG['min_position_size'] is 100, not 10"): this
    # strategy only signals when `yes_ask + no_ask` is low, so the
    # intent's notional (`min_position_size * (yes_ask + no_ask)`) is
    # SMALLEST exactly where the strategy is most profitable, and a
    # default sized against a high, non-signaling price cleared
    # `settings.min_trade_usd` ($10) on paper while never clearing it
    # at any price this strategy actually trades. 100 clears the floor
    # across the strategy's entire realistic signaling range AND is the
    # size at which the fixed `2 * redemption_gas_usd` cost is properly
    # amortized rather than merely legal (net-per-contract at 100 is
    # already within ~2% of its asymptotic value at unlimited scale).
    "min_position_size": 100.0,
    # Hard cap on the leg size in contracts (same units as
    # `min_position_size`), applied before the engine's own
    # cash/`max_position_pct` scaling.
    "max_position_size": 1000.0,
    # Minimum 24h volume (USD notional) required to consider a market.
    "min_liquidity": 1000.0,
}


def _fee_inputs(venue: VenueId, category: str | None) -> tuple[FeeModel, FeeSchedule]:
    """Return the `(FeeModel, FeeSchedule)` to price a taker fill on `venue`.

    Never a literal fee rate (GUARDRAILS.md §1.5): Polymarket's rate
    comes from `category_fee_schedule(category)` (itself checking
    `settings.polymarket_taker_fee_overrides` first, then the category
    table, and labeling the schedule's `source` accordingly — Phase-1
    remediation FIX 3); Kalshi's comes from `Settings` via
    `default_kalshi_schedule()`.

    Args:
        venue: `"polymarket"` or `"kalshi"`.
        category: The market's category (Polymarket only; ignored for
            Kalshi).

    Returns:
        tuple[FeeModel, FeeSchedule]: The model to call `.fee()` on and
            the schedule to pass it.
    """
    if venue == "kalshi":
        return KalshiFeeModel(), default_kalshi_schedule()
    return PolymarketFeeModel(), category_fee_schedule(category)


def _sizing_ask(
    snapshot: MarketSnapshot, *, outcome: str, top_of_book: float, size: float
) -> float:
    """Return the price at which `size` contracts of `outcome` can be bought.

    Walks `snapshot.book` (best-first, via `OrderBook.walk`) when it is
    present and recorded for this exact `outcome`, returning the
    size-weighted average price over however much of `size` the book
    can actually supply. This is deliberately NOT top-of-book once real
    depth exists — a large order does not fill entirely at the best
    price. Falls back to `top_of_book` (the snapshot's plain
    `yes_ask`/`no_ask` quote) when there is no matching book, which is
    the only information available pre-T10/pre-book-collection.

    Args:
        snapshot: The snapshot to read `book` from.
        outcome: `"YES"` or `"NO"` — must match `snapshot.book.outcome`
            (case-insensitively) for the walk to be used.
        top_of_book: Fallback ask price when no matching book is present.
        size: Contracts to price, `>= 0`.

    Returns:
        float: The size-weighted ask, or `top_of_book` if the book is
            absent, mismatched, or has no ask depth at all.
    """
    book = snapshot.book
    if book is None or book.outcome.casefold() != outcome.casefold():
        return top_of_book
    fills = book.walk("buy", size)
    if not fills:
        return top_of_book
    total_size = sum(qty for _, qty in fills)
    if total_size <= 0.0:
        return top_of_book
    total_cost = sum(price * qty for price, qty in fills)
    return total_cost / total_size


class BinaryComplementArbitrageStrategy(BaseStrategy):
    """Arbitrage strategy exploiting YES + NO ask < 1.0 mispricings.

    In a binary market, buying one YES contract and one NO contract
    always redeems for exactly $1.00 (one side pays $1, the other $0).
    When `yes_ask + no_ask` is low enough that the per-contract edge
    survives taker fees and amortized redemption gas (see module
    docstring), this signals a `kind="complement"` `Intent` that buys
    both legs and holds them to resolution.
    """

    name = "binary_complement_arbitrage"
    description = "Arbitrage when YES ask + NO ask < 1.0, net of costs"
    version = "2.0.0"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize with merged config."""
        merged_config = {**DEFAULT_CONFIG, **(config or {})}
        super().__init__(merged_config)
        self._opportunities_found = 0
        self._total_theoretical_profit = 0.0

    def on_market_data(self, snapshot: MarketSnapshot) -> Intent | None:
        """Check for a complement arbitrage opportunity.

        Args:
            snapshot: Current market state.

        Returns:
            Intent | None: A 2-leg `kind="complement"` `Intent` (YES +
                NO, both BUY, held to resolution) if the net edge clears
                `min_profit_margin`; `None` otherwise.
        """
        if snapshot.volume_24h < self.config["min_liquidity"]:
            return None

        size = float(self.config["min_position_size"])
        yes_top = snapshot.yes_ask if snapshot.yes_ask is not None else snapshot.yes_price
        no_top = snapshot.no_ask if snapshot.no_ask is not None else snapshot.no_price
        yes_ask = _sizing_ask(snapshot, outcome="YES", top_of_book=yes_top, size=size)
        no_ask = _sizing_ask(snapshot, outcome="NO", top_of_book=no_top, size=size)

        fee_model, schedule = _fee_inputs(snapshot.venue, snapshot.category)
        yes_fee = fee_model.fee(yes_ask, 1.0, "taker", schedule)
        no_fee = fee_model.fee(no_ask, 1.0, "taker", schedule)
        gas_per_contract = (2.0 * settings.redemption_gas_usd) / size

        gross_edge = 1.0 - (yes_ask + no_ask)
        edge = gross_edge - yes_fee - no_fee - gas_per_contract

        if edge < self.config["min_profit_margin"]:
            return None

        self._opportunities_found += 1
        self._total_theoretical_profit += edge

        leg_size = min(size, float(self.config["max_position_size"]))
        confidence = max(0.0, min(edge / 0.10, 1.0))

        return Intent(
            kind="complement",
            legs=[
                Leg(
                    market_id=snapshot.market_id,
                    outcome="YES",
                    side="BUY",
                    limit_price=yes_ask,
                    size_contracts=leg_size,
                    venue=snapshot.venue,
                ),
                Leg(
                    market_id=snapshot.market_id,
                    outcome="NO",
                    side="BUY",
                    limit_price=no_ask,
                    size_contracts=leg_size,
                    venue=snapshot.venue,
                ),
            ],
            hold_to_resolution=True,
            atomicity="all_or_none",
            confidence=confidence,
            expected_resolution_ts=snapshot.end_date,
            metadata={
                "strategy": self.name,
                "yes_ask": yes_ask,
                "no_ask": no_ask,
                "gross_edge": gross_edge,
                "yes_fee": yes_fee,
                "no_fee": no_fee,
                "gas_per_contract": gas_per_contract,
                "edge": edge,
                "fee_schedule_source": schedule.source,
                "is_arbitrage": True,
            },
        )

    def calculate_position_size(
        self,
        signal: Signal,
        portfolio_value: float,
        positions: dict[str, Any],  # noqa: ARG002 - interface parity, see below
    ) -> float:
        """Return a dollar sizing fallback (interface compliance only).

        Not consulted by the backtest engine for this strategy's own
        intents: `on_market_data` always sets `Leg.size_contracts`
        directly on both legs, and `Backtester._leg_sizes` honors
        explicit `size_contracts` verbatim over any USD budget from this
        method (see module docstring on why the two legs must be sized
        equal in CONTRACTS). Implemented anyway because `BaseStrategy`
        requires it and other callers (e.g. a UI sizing probe) may still
        invoke it directly.

        Args:
            signal: A `Signal` describing the leg to size (its `.price`
                is the leg's limit price).
            portfolio_value: Current total portfolio value.
            positions: Current positions (unused; kept for interface
                parity with `BaseStrategy.calculate_position_size`).

        Returns:
            float: Position size in dollars, `>= 0`.
        """
        price = signal.price if signal.price > 0.0 else 0.5
        max_by_pct = portfolio_value * float(self.config["max_position_pct"])
        dollar_cap = float(self.config["max_position_size"]) * price
        position_size = min(max_by_pct, dollar_cap)
        position_size = max(position_size, float(self.config["min_position_size"]) * price)
        position_size *= signal.confidence
        return min(position_size, portfolio_value * 0.5)

    def reset(self) -> None:
        """Reset strategy state."""
        super().reset()
        self._opportunities_found = 0
        self._total_theoretical_profit = 0.0

    def get_stats(self) -> dict[str, Any]:
        """Get strategy statistics."""
        stats = super().get_stats()
        stats.update({
            "opportunities_found": self._opportunities_found,
            "total_theoretical_profit": self._total_theoretical_profit,
        })
        return stats
