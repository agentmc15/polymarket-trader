"""When does a RESTING quote get filled, and what did it cost?

`SimulatedFillEngine` is a taker engine: it walks a book and crosses it,
so it always fills at someone else's price. A maker's question is the
opposite one — my order sits at MY price and fills only if the market
comes to me — and nothing here modelled it, which is why market making
could not be evaluated at all.

THE PROBLEM THIS MODEL CANNOT SOLVE, and therefore reports instead. A
public trade tape says a trade printed at a price; it does not say
whether YOUR order was the one that filled. Between you and the fill sits
the queue, and its depth is not in any historical record this repo can
read. So the engine does not guess: it takes an explicit `FillModel` and
the caller states the assumption.

    OPTIMISTIC -- a print AT your price fills you. True only if you are
                  at the front of the queue.
    PESSIMISTIC -- only a print strictly THROUGH your price fills you, so
                  everything resting at that price had to clear first.
                  Close to a newcomer's reality.

That gap is not a detail to be tuned away; it is the entire result.
Measured on 34,137 hourly Kalshi candles, the same passive strategy earns
+0.0051 per fill under the optimistic model and LOSES 0.0095 under the
pessimistic one. Any evaluation quoting one number without naming its
fill model is quoting a number that does not exist — the same discipline
GUARDRAILS.md §7 already imposes on `depth_source`/`fill_at`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from app.strategies.market_making import Quote, QuotePair
from app.venues.fees import FeeModel
from app.venues.types import FeeSchedule

#: How a resting order is assumed to interact with the queue ahead of it.
FillModel = Literal["optimistic", "pessimistic"]

#: Recorded on every fill, so a P&L number can never be read without the
#: assumption that produced it (GUARDRAILS.md §7's rule, applied to the
#: maker side).
FILL_MODEL_KEY = "fill_model"


@dataclass(frozen=True)
class PassiveFill:
    """One resting order that traded.

    Attributes:
        side: `"buy"` or `"sell"`.
        price: The price the resting order was posted at, and therefore
            filled at — a maker fills at its OWN price, which is the
            whole point of being one.
        size: Contracts filled.
        fee: Maker fee in USD for this fill, from the venue's fee model.
        fill_model: The queue assumption that produced this fill.
    """

    side: str
    price: float
    size: float
    fee: float
    fill_model: FillModel


@dataclass(frozen=True)
class TradeRange:
    """The trades that printed in one interval, as a range.

    Attributes:
        low: Lowest trade price in the interval.
        high: Highest trade price in the interval.
        volume: Contracts traded. Zero means nothing printed, and no
            resting order can have filled.
    """

    low: float
    high: float
    volume: float

    def __post_init__(self) -> None:
        """Validate the range.

        Raises:
            ValueError: If the bounds are not finite probabilities with
                `low <= high`, or `volume` is negative.
        """
        for name, value in (("low", self.low), ("high", self.high)):
            if not (math.isfinite(value) and 0.0 <= value <= 1.0):
                raise ValueError(f"{name} must be a probability, got {value!r}")
        if self.low > self.high:
            raise ValueError(f"low {self.low!r} exceeds high {self.high!r}")
        if not (math.isfinite(self.volume) and self.volume >= 0.0):
            raise ValueError(f"volume must be finite and >= 0, got {self.volume!r}")


class PassiveFillEngine:
    """Decides which resting quotes traded against a period's prints."""

    def __init__(
        self,
        fee_model: FeeModel,
        schedule: FeeSchedule,
        *,
        fill_model: FillModel = "pessimistic",
    ) -> None:
        """Configure the engine.

        Args:
            fee_model: The venue's fee model. Fees are never literals
                here (GUARDRAILS.md §1.5) — and the maker rate is the
                whole economic case for this side of the trade, so it is
                sourced, not assumed.
            schedule: The market's fee schedule.
            fill_model: Queue assumption. Defaults to `"pessimistic"`,
                because assuming queue priority you have not earned is
                the failure that turns a losing strategy into a winning
                backtest.
        """
        self.fee_model = fee_model
        self.schedule = schedule
        self.fill_model = fill_model

    def fills(self, pair: QuotePair, trades: TradeRange) -> list[PassiveFill]:
        """Return the fills `pair` would have taken against `trades`.

        A buy rests below the market and fills when prints reach DOWN to
        it; a sell rests above and fills when prints reach UP.

        Args:
            pair: The quote that was resting.
            trades: What printed while it rested.

        Returns:
            list[PassiveFill]: One entry per side that filled, empty if
                neither did. Both sides can fill in one interval — that
                is the round trip the strategy exists to earn.
        """
        if trades.volume <= 0.0:
            return []
        out: list[PassiveFill] = []
        for quote in pair.quotes:
            if self._filled(quote, trades):
                out.append(
                    PassiveFill(
                        side=quote.side,
                        price=quote.price,
                        size=quote.size,
                        fee=self.fee_model.fee(
                            quote.price, quote.size, "maker", self.schedule
                        ),
                        fill_model=self.fill_model,
                    )
                )
        return out

    def _filled(self, quote: Quote, trades: TradeRange) -> bool:
        """Whether one resting quote traded, under this engine's model."""
        if quote.side == "buy":
            return (
                trades.low <= quote.price
                if self.fill_model == "optimistic"
                else trades.low < quote.price
            )
        return (
            trades.high >= quote.price
            if self.fill_model == "optimistic"
            else trades.high > quote.price
        )


def mark_to_market(fill: PassiveFill, mark: float) -> float:
    """Return one fill's P&L per the mark, net of its fee.

    The mark must come from AFTER the interval the fill happened in.
    Marking inside it scores the trade against a price the fill itself
    helped set, which flatters every result — it is the maker-side twin
    of scoring a calibration study against the settlement price.

    Args:
        fill: The fill to value.
        mark: Fair value after the fill, a probability in [0.0, 1.0].

    Returns:
        float: P&L in USD, negative for a loss.

    Raises:
        ValueError: If `mark` is not a finite probability.
    """
    if not (math.isfinite(mark) and 0.0 <= mark <= 1.0):
        raise ValueError(f"mark must be a probability, got {mark!r}")
    direction = 1.0 if fill.side == "buy" else -1.0
    return direction * (mark - fill.price) * fill.size - fill.fee
