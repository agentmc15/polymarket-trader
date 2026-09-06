"""Multi-outcome bundle arbitrage strategy.

Exploits mispricing when the sum of all outcome asks in a multi-outcome
market (>= 3 mutually exclusive outcomes) is less than 1.0 net of fees,
by buying every outcome and holding to resolution — exactly one
outcome always redeems for $1.00 per contract, the rest for $0.00, so
the edge computed here is realized regardless of which outcome wins.

GUARDRAILS.md §1.5: fees are never literals here — every fee comes from
`app.venues.fees.PolymarketFeeModel`/`KalshiFeeModel` via a
`FeeSchedule` built from `snapshot.category` (Polymarket) or `Settings`
(Kalshi), one call to `.fee()` per outcome leg (PLAN.md §3: the fee
formula is per-contract, so a bundle's per-leg fees are summed, not
averaged or charged once for the whole bundle).

This strategy requires the full per-outcome orderbook
(`snapshot.orderbook["outcomes"]`, `{name: {"ask": ..., "liquidity":
...}}` or `{name: ask_price}`). Unlike the pre-T10 version, a market
that does not expose at least `min_outcomes` (default 3) priced
outcomes returns `None` outright — there is no "fall back to the
binary YES/NO quotes" path: a 2-outcome market is what
`binary_complement_arbitrage` is for, and silently treating a
2-outcome market as a degenerate bundle previously let an incomplete
payload masquerade as a real >=3-outcome opportunity.

**Why `DEFAULT_CONFIG["min_position_size"]` is 100, not 10** (T10
retry defect fix — GUARDRAILS.md, same shape of bug as
`binary_complement_arbitrage`): the backtest/paper engine rejects any
intent whose total notional (`size_contracts * sum of leg limit
prices`, here `min_position_size * sum(outcome asks)`) is below
`settings.min_trade_usd` (default $10 — see
`app.services.backtesting.engine._leg_sizes`). This strategy only
signals when the outcome asks sum to LESS than 1.0 by more than
`min_profit_margin` — a bigger edge means a SMALLER sum of asks, hence
a SMALLER notional per contract at the same size, not a larger one.
The shipped example fixture (4 outcomes summing to 0.75, a fairly large
25% gross edge) already fails the floor at the old default: `10 * 0.75
= $7.50 < $10`. At `min_position_size = 100`, `100 * 0.75 = $75` clears
it with room to spare, and the floor is still cleared down to an
outcome-ask sum of `~0.10` (a 90% gross edge, well outside any
plausible real mispricing across `min_outcomes` to `max_outcomes`
legs). Kept equal to `binary_complement_arbitrage`'s default for the
same reason: 100 contracts is a small, easily-amortized fixed cost
against ordinary per-market liquidity (`min_liquidity_per_outcome` is
$500), not an outsized position. Note this strategy does not fold a
redemption-gas cost into `profit_margin` the way
`binary_complement_arbitrage` folds `2 * redemption_gas_usd` into its
per-contract edge (there is no fixed-cost amortization to size against
here) — only the notional floor drove this default; if bundle gas
accounting is added later, re-derive the default the same way the
sibling strategy documents.
"""
from typing import Any

from app.strategies.base import (
    EDGE_BASIS_KEY,
    EDGE_BASIS_OBSERVED,
    SCORING_EDGE_KEY,
    BaseStrategy,
    Intent,
    Leg,
    MarketSnapshot,
    Signal,
)
from app.venues.base import FeeModel
from app.venues.fees import (
    KalshiFeeModel,
    PolymarketFeeModel,
    category_fee_schedule,
    default_kalshi_schedule,
)
from app.venues.types import FeeSchedule

DEFAULT_CONFIG: dict[str, Any] = {
    # Minimum edge, per contract, net of per-outcome taker fees, to signal.
    "min_profit_margin": 0.03,
    # Maximum notional for the intent as a fraction of portfolio equity.
    "max_position_pct": 0.08,
    # CONTRACTS, not dollars: the size committed to every leg (equal
    # across outcomes — see `on_market_data`), capped by
    # `max_position_size` below and by the engine's own cash scaling.
    #
    # 100, NOT 10 (T10 retry defect fix — see module docstring's "Why
    # DEFAULT_CONFIG['min_position_size'] is 100, not 10"): the old
    # default of 10 produced a notional (`10 * sum(outcome asks)`)
    # below the engine's `settings.min_trade_usd` ($10) floor at every
    # sum-of-asks this strategy would ever actually signal at, since a
    # bigger edge means a SMALLER sum of asks, not a larger one.
    "min_position_size": 100.0,
    # Hard cap on the leg size in contracts.
    "max_position_size": 500.0,
    # Minimum number of outcomes to consider (a bundle Intent requires
    # at least 3 legs; see `app.strategies.base.Intent.__post_init__`).
    "min_outcomes": 3,
    # Maximum number of outcomes (complexity limit).
    "max_outcomes": 10,
    # Minimum liquidity required per outcome (USD notional, or contracts
    # depending on the payload's own "liquidity" units).
    "min_liquidity_per_outcome": 500.0,
}


class MultiOutcomeBundleArbitrageStrategy(BaseStrategy):
    """Arbitrage strategy for multi-outcome (>= 3 outcome) markets.

    In a market with N mutually exclusive outcomes, the sum of every
    outcome's ask should be close to 1.0. When the sum is low enough
    that the per-contract edge survives every outcome's taker fee, this
    signals a `kind="bundle"` `Intent` — one BUY leg per outcome, held
    to resolution.
    """

    name = "multi_outcome_bundle_arbitrage"
    description = "Arbitrage across multi-outcome (>= 3 outcome) markets"
    version = "2.0.0"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize with merged config."""
        merged_config = {**DEFAULT_CONFIG, **(config or {})}
        super().__init__(merged_config)
        self._bundle_opportunities = 0
        self._markets_analyzed: dict[str, dict[str, Any]] = {}

    def on_market_data(self, snapshot: MarketSnapshot) -> Intent | None:
        """Analyze a multi-outcome market for bundle arbitrage.

        Args:
            snapshot: Current market state. `snapshot.orderbook` must
                carry an `"outcomes"` mapping of outcome name to either
                a price (`float`) or a dict with an `"ask"` (or
                `"price"`) key and optionally `"liquidity"`.

        Returns:
            Intent | None: A `kind="bundle"` `Intent` (one BUY leg per
                outcome, held to resolution) if every outcome is priced,
                there are at least `min_outcomes` of them, and the net
                edge clears `min_profit_margin`; `None` otherwise
                (including when the payload is missing any outcome's
                ask, or has fewer than `min_outcomes` outcomes — no
                fallback to a binary YES/NO check).
        """
        outcomes = snapshot.orderbook.get("outcomes", {}) if snapshot.orderbook else {}
        if not outcomes:
            return None

        num_outcomes = len(outcomes)
        if num_outcomes < self.config["min_outcomes"]:
            return None
        if num_outcomes > self.config["max_outcomes"]:
            return None

        outcome_prices: dict[str, float] = {}
        for outcome_name, outcome_data in outcomes.items():
            if isinstance(outcome_data, dict):
                price = outcome_data.get("ask", outcome_data.get("price"))
                liquidity = outcome_data.get("liquidity", 0.0)
            else:
                price = float(outcome_data)
                liquidity = self.config["min_liquidity_per_outcome"]

            if price is None:
                # Missing ask for this outcome: no fallback, no partial
                # bundle — the whole market is unpriceable this tick.
                return None
            if liquidity < self.config["min_liquidity_per_outcome"]:
                return None

            outcome_prices[outcome_name] = float(price)

        fee_model: FeeModel
        schedule: FeeSchedule
        if snapshot.venue == "kalshi":
            fee_model, schedule = KalshiFeeModel(), default_kalshi_schedule()
        else:
            fee_model = PolymarketFeeModel()
            schedule = category_fee_schedule(snapshot.category)

        total_cost = 0.0
        total_fees = 0.0
        per_outcome_fees: dict[str, float] = {}
        for outcome_name, price in outcome_prices.items():
            fee = fee_model.fee(price, 1.0, "taker", schedule)
            per_outcome_fees[outcome_name] = fee
            total_cost += price
            total_fees += fee

        profit_margin = 1.0 - total_cost - total_fees

        if profit_margin < self.config["min_profit_margin"]:
            return None

        self._bundle_opportunities += 1
        self._markets_analyzed[snapshot.market_id] = {
            "outcomes": outcome_prices,
            "total_cost": total_cost,
            "total_fees": total_fees,
            "profit_margin": profit_margin,
            "timestamp": snapshot.timestamp,
        }

        leg_size = min(
            float(self.config["min_position_size"]),
            float(self.config["max_position_size"]),
        )
        confidence = max(0.0, min(profit_margin / 0.10, 1.0))

        legs = [
            Leg(
                market_id=snapshot.market_id,
                outcome=outcome_name,
                side="BUY",
                limit_price=price,
                size_contracts=leg_size,
                venue=snapshot.venue,
            )
            for outcome_name, price in outcome_prices.items()
        ]

        return Intent(
            kind="bundle",
            legs=legs,
            hold_to_resolution=True,
            atomicity="all_or_none",
            confidence=confidence,
            expected_resolution_ts=snapshot.end_date,
            metadata={
                "strategy": self.name,
                "is_bundle_arb": True,
                "num_outcomes": num_outcomes,
                "outcome_prices": outcome_prices,
                "outcome_fees": per_outcome_fees,
                "total_cost": total_cost,
                "total_fees": total_fees,
                "profit_margin": profit_margin,
                # THE SCORING CONTRACT (T31, `app.strategies.base`):
                # `profit_margin` net of every outcome's taker fee
                # (subtracted above) and of nothing else — no gas, and no
                # probability haircut, because a bundle is single-venue
                # and single-market: every leg redeems off the same
                # question, so there is no identity risk to price.
                #
                # This comment used to end "Do not compare this value to
                # `cross_venue_arbitrage`'s `net_edge` as if they
                # measured the same kind of risk" — and `composite`, its
                # only consumer, did exactly that, because that strategy
                # published a link-confidence-haircut number under the
                # same key. T31 removed the trap rather than the warning:
                # cross-venue now publishes its PRE-haircut edge under
                # this same `SCORING_EDGE_KEY` and declares its
                # confidence separately, so the two keys finally do mean
                # the same thing and `app.services.scoring` applies the
                # one haircut that differs.
                #
                # `"net_edge"` is kept as an alias of the same value
                # (T21b published under that name; persisted rows and
                # `SCORING_EDGE_LEGACY_KEY` still read it) — it is the
                # identical number, not a second quantity.
                SCORING_EDGE_KEY: profit_margin,
                "net_edge": profit_margin,
                EDGE_BASIS_KEY: EDGE_BASIS_OBSERVED,
                "fee_schedule_source": schedule.source,
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
        intents: `on_market_data` sets `Leg.size_contracts` directly on
        every leg (equal across outcomes), and `Backtester._leg_sizes`
        honors explicit `size_contracts` verbatim over any USD budget
        from this method. Implemented anyway because `BaseStrategy`
        requires it.

        Args:
            signal: A `Signal` describing one leg to size.
            portfolio_value: Current total portfolio value.
            positions: Current positions (unused; kept for interface
                parity with `BaseStrategy.calculate_position_size`).

        Returns:
            float: Position size in dollars, `>= 0`.
        """
        num_outcomes = int(signal.metadata.get("num_outcomes", 3))
        max_by_pct = portfolio_value * float(self.config["max_position_pct"])
        position_size = min(max_by_pct, float(self.config["max_position_size"]))
        position_size = max(position_size, float(self.config["min_position_size"]))
        position_size *= signal.confidence
        position_size = min(position_size, portfolio_value * 0.4)
        return position_size / max(num_outcomes, 1)

    def reset(self) -> None:
        """Reset strategy state."""
        super().reset()
        self._bundle_opportunities = 0
        self._markets_analyzed.clear()

    def get_stats(self) -> dict[str, Any]:
        """Get strategy statistics."""
        stats = super().get_stats()
        stats.update({
            "bundle_opportunities": self._bundle_opportunities,
            "markets_analyzed": len(self._markets_analyzed),
        })
        return stats
