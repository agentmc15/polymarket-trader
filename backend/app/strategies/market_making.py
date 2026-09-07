"""Two-sided passive quoting, calibrated against measured adverse selection.

WHY THIS EXISTS. Every taker strategy this kit has measured loses to the
spread, and the losses are monotone in it: crossing the widest third of
Kalshi books costs -0.2580 per contract, the tightest -0.0217. The spread
is the product, and the taker pays it. This is the other side of that
trade — Kalshi charges takers 7% and makers nothing, so a resting quote
collects the spread with no fee drag.

THE QUESTION THAT DECIDES IT is not spread width but ADVERSE SELECTION:
you are filled precisely when someone better informed wants the other
side. Measured on 34,137 hourly candles across 537 settled Kalshi
markets, decomposing each passive fill into what was quoted and what
survived one hour:

    fill model         fills   quoted half   realized half   adverse
    front of queue     7,857     +0.0182       +0.0051        72%
    behind the queue   3,046     +0.0236       -0.0095       140%

So the whole thesis turns on queue position, which is exactly the thing a
newcomer does not control — and the honest conclusion is that quoting
indiscriminately is a coin flip on execution quality.

WHAT SURVIVES BOTH MODELS. Splitting by quoted spread (realized per fill,
95% CI clustered by event):

    spread      front of queue            behind the queue
    <= 0.02     +0.0000 [-.0016,+.0016]   -0.0156 [-.0205,-.0096]
    0.02-0.05   +0.0012 [-.0027,+.0044]   -0.0153 [-.0241,-.0066]
    0.05-0.10   +0.0095 [+.0006,+.0175]   -0.0064 [-.0214,+.0077]
    0.10-0.25   +0.0348 [+.0198,+.0476]   +0.0143 [-.0075,+.0358]
    >= 0.25     +0.1152 [+.0867,+.1465]   +0.0735 [+.0270,+.1225]

Only the widest bucket is profitable under BOTH. `DEFAULT_MIN_SPREAD` is
therefore 0.10 and `CONSERVATIVE_MIN_SPREAD` is 0.25 — the point below
which profit requires an execution assumption this repo cannot yet make
good on. Tight books are not a smaller opportunity here; they are a
losing one.

INVENTORY IS NOT SYMMETRIC, and that is a measured fact rather than a
modeling convenience. Sell fills outnumbered buy fills 1.5x to 2.0x in
EVERY price bucket, including 0.30-0.70 where a floor at zero cannot
explain it: takers on prediction markets are net BUYERS of YES. A
two-sided quoter therefore accumulates a short position by default, and
the skew below is what stops that drift from becoming an unhedged
directional bet against the crowd.

THIS MODULE DECIDES ONLY WHAT TO QUOTE. It places nothing (GUARDRAILS.md
§1.1), and it is a pure function of book, inventory and parameters, so
its policy can be replayed against history by
`app.execution.passive_fill` without touching a venue.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from app.venues.types import OrderBook, VenueId

#: Below this quoted spread, passive quoting did not pay under the
#: optimistic fill model either (+0.0012, CI spanning zero at 0.02-0.05).
#: The first bucket with a positive lower bound at the front of the queue
#: is 0.05-0.10; 0.10 is the first that is comfortably positive there and
#: not negative behind the queue.
DEFAULT_MIN_SPREAD = 0.10

#: The only bucket profitable under BOTH fill models. Use this when queue
#: priority cannot be assumed, which for a new participant is the honest
#: default.
CONSERVATIVE_MIN_SPREAD = 0.25

#: Fraction of the spread to keep as edge when improving the touch. At
#: 1.0 the quote sits ON the touch; at 0.5, halfway between touch and
#: mid.
#:
#: CALIBRATED, and it is the parameter that carries the whole result.
#: Sweeping it on 537 markets (min_spread 0.10, max_inventory 20, return
#: on capital per quoted market-hour):
#:
#:     edge   traded   mean P&L     ROC
#:     0.50      196    +0.2760   +0.0137
#:     0.70      190    +0.8718   +0.0474
#:     0.80      184    +1.0878   +0.0612   <- broad optimum
#:     0.90      160    +1.1434   +0.0597
#:     1.00      133    +0.6395   +0.0287
#:
#: The optimum is interior for a reason worth keeping: quoting too close
#: to the mid (low values) fills often but hands most of the spread back
#: as adverse selection, while quoting AT the touch (1.0) earns no queue
#: priority and simply trades less — 133 markets against 184. Verified
#: out of sample and across 60 independent random halves, where 0.80 beat
#: the old 0.5 default on 60 of 60 draws.
DEFAULT_EDGE_FRACTION = 0.80

#: Inventory at which one side is withdrawn entirely, in contracts.
#:
#: CALIBRATED to 20 (was 100): a tight limit is the primary risk control,
#: and it is a SUBSTITUTE for `skew_strength` rather than a complement.
#: Worst single-market P&L, edge_fraction 0.80:
#:
#:     max_inventory   skew 0.0   skew 1.0   skew 2.0
#:              20       -8.90      -6.90      -6.90
#:             100      -46.25     -13.25     -11.60
#:
#: At 100 the skew is what stands between the book and a -46 market; at
#: 20 the withdrawal does that job already. Raising this WITHOUT raising
#: `skew_strength` re-opens exactly that tail.
DEFAULT_MAX_INVENTORY = 20.0

#: How hard inventory pushes the quote, as a fraction of the half-spread
#: at full inventory. At 1.0 a maxed-out book shifts its quotes by a full
#: half-spread toward getting flat.
#:
#: DELIBERATELY LEFT AT 1.0, against the grid search. At the calibrated
#: `DEFAULT_MAX_INVENTORY` of 20 the measured trade-off is:
#:
#:     skew   mean P&L      ROC   5th pct   worst
#:     0.0     +1.0878   +0.0612    -5.200   -8.90
#:     1.0     +0.6514   +0.0348    -4.700   -6.90
#:
#: Zero earns 1.7x more and gives up a slightly worse tail — a risk
#: appetite, not a fact, and not one this module should silently spend on
#: an operator's behalf. It is also the parameter this backtest measures
#: worst: hourly candles cannot see intra-hour inventory swings, so the
#: value of leaning against them is understated here by construction.
#: An operator running the diversified portfolio this strategy needs
#: (~2,500 simultaneous markets, where per-market tails average out) has
#: a good case for lowering it; that is their call to make explicitly.
DEFAULT_SKEW_STRENGTH = 1.0


@dataclass(frozen=True)
class Quote:
    """One side of a two-sided quote.

    Attributes:
        side: `"buy"` or `"sell"`.
        price: Limit price, a probability in [0.0, 1.0], already rounded
            to the market's tick.
        size: Size in contracts, `> 0`.
    """

    side: str
    price: float
    size: float


@dataclass(frozen=True)
class QuotePair:
    """What the policy wants resting in one market right now.

    Either side may be `None` — that is a decision, not a failure: an
    inventory limit withdraws the side that would make it worse, and a
    book too tight to pay withdraws both.

    Attributes:
        venue: Venue the quote belongs to.
        market_id: Venue-native market identifier.
        outcome: Outcome being quoted.
        bid: The resting buy, or `None`.
        ask: The resting sell, or `None`.
        reason: Why the policy produced this, for the log and for a
            human reading a paper-trading run.
        metadata: Book state the decision was made from.
    """

    venue: VenueId
    market_id: str
    outcome: str
    bid: Quote | None
    ask: Quote | None
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_two_sided(self) -> bool:
        """Whether both sides are being quoted."""
        return self.bid is not None and self.ask is not None

    @property
    def quotes(self) -> tuple[Quote, ...]:
        """The sides actually being quoted, in (bid, ask) order."""
        return tuple(q for q in (self.bid, self.ask) if q is not None)


def round_to_tick(price: float, tick: float, *, side: str) -> float:
    """Round `price` to `tick` in the direction that is CONSERVATIVE.

    A bid rounds DOWN and an ask rounds UP, so rounding can only ever
    widen the quote. Rounding a bid up would pay more than the policy
    decided to pay, which is the direction that silently loses money.

    Args:
        price: Unrounded limit price.
        tick: The market's minimum price increment, `> 0`.
        side: `"buy"` or `"sell"`.

    Returns:
        float: The rounded price, clamped to [0.0, 1.0].

    Raises:
        ValueError: If `tick` is not finite and positive.
    """
    if not (math.isfinite(tick) and tick > 0.0):
        raise ValueError(f"tick must be finite and > 0, got {tick!r}")
    steps = price / tick
    rounded = (math.floor(steps) if side == "buy" else math.ceil(steps)) * tick
    # `floor`/`ceil` on a binary float can land a hair outside; the clamp
    # keeps the result a probability, which every downstream type demands.
    return min(max(round(rounded, 10), 0.0), 1.0)


class MarketMaker:
    """Decides the two-sided quote for one market.

    Stateless with respect to the venue: inventory is passed in, so the
    same policy object can be replayed over history or driven live
    without behaving differently.
    """

    def __init__(
        self,
        *,
        min_spread: float = DEFAULT_MIN_SPREAD,
        edge_fraction: float = DEFAULT_EDGE_FRACTION,
        max_inventory: float = DEFAULT_MAX_INVENTORY,
        quote_size: float = 10.0,
        skew_strength: float = DEFAULT_SKEW_STRENGTH,
    ) -> None:
        """Configure the policy.

        Args:
            min_spread: Minimum quoted spread to quote into at all. See
                the module docstring: below 0.10 passive quoting did not
                pay even at the front of the queue.
            edge_fraction: Fraction of the half-spread kept as edge when
                improving the touch, in (0.0, 1.0].
            max_inventory: Absolute contract position at which the side
                that would increase it is withdrawn.
            quote_size: Contracts per side.
            skew_strength: How hard inventory shifts the quote.

        Raises:
            ValueError: If any parameter is outside its documented range.
        """
        if not 0.0 < edge_fraction <= 1.0:
            raise ValueError(f"edge_fraction must be in (0, 1], got {edge_fraction!r}")
        if not min_spread > 0.0:
            raise ValueError(f"min_spread must be > 0, got {min_spread!r}")
        if not max_inventory > 0.0:
            raise ValueError(f"max_inventory must be > 0, got {max_inventory!r}")
        if not quote_size > 0.0:
            raise ValueError(f"quote_size must be > 0, got {quote_size!r}")
        if skew_strength < 0.0:
            raise ValueError(f"skew_strength must be >= 0, got {skew_strength!r}")
        self.min_spread = min_spread
        self.edge_fraction = edge_fraction
        self.max_inventory = max_inventory
        self.quote_size = quote_size
        self.skew_strength = skew_strength

    def quote(
        self,
        book: OrderBook,
        *,
        tick_size: float,
        inventory: float = 0.0,
    ) -> QuotePair:
        """Return the quote this policy wants resting in `book`.

        Args:
            book: Current order book for one (market, outcome).
            tick_size: The market's minimum price increment.
            inventory: Signed current position in contracts — positive is
                long, negative short. Skews the quote toward flat.

        Returns:
            QuotePair: Possibly with one or both sides `None`; see
                `QuotePair` for why that is an answer rather than a
                failure.
        """
        best_bid, best_ask = book.best_bid(), book.best_ask()
        base = {
            "venue": book.venue,
            "market_id": book.market_id,
            "outcome": book.outcome,
        }
        if best_bid is None or best_ask is None:
            # A one-sided book gives no mid, and inventing one would be
            # inventing the fair value this whole policy prices against.
            return QuotePair(**base, bid=None, ask=None,
                             reason="one_sided_book", metadata={})

        spread = best_ask.price - best_bid.price
        mid = (best_ask.price + best_bid.price) / 2.0
        meta: dict[str, Any] = {
            "best_bid": best_bid.price,
            "best_ask": best_ask.price,
            "spread": spread,
            "mid": mid,
            "inventory": inventory,
            "min_spread": self.min_spread,
        }
        if spread <= 0.0:
            # Crossed or locked: not a market to quote into.
            return QuotePair(**base, bid=None, ask=None,
                             reason="crossed_or_locked_book", metadata=meta)
        if spread < self.min_spread:
            return QuotePair(**base, bid=None, ask=None,
                             reason="spread_below_minimum", metadata=meta)

        # Improve the touch by keeping `edge_fraction` of the half-spread.
        half_edge = (spread / 2.0) * self.edge_fraction
        # Inventory skew: long inventory pushes BOTH quotes down, so the
        # ask is likelier to fill and the bid less so. Measured on live
        # data, sell fills outnumber buy fills roughly 1.5-2x, so without
        # this a two-sided quoter drifts short by construction.
        lean = 0.0
        if self.max_inventory > 0.0:
            clamped = max(-1.0, min(1.0, inventory / self.max_inventory))
            lean = clamped * half_edge * self.skew_strength
        bid_price = round_to_tick(mid - half_edge - lean, tick_size, side="buy")
        ask_price = round_to_tick(mid + half_edge - lean, tick_size, side="sell")
        meta.update({"half_edge": half_edge, "lean": lean,
                     "bid_price": bid_price, "ask_price": ask_price})

        bid: Quote | None = Quote("buy", bid_price, self.quote_size)
        ask: Quote | None = Quote("sell", ask_price, self.quote_size)
        reason = "quoting_two_sided"

        # An inventory limit withdraws the side that would breach it.
        if inventory >= self.max_inventory:
            bid, reason = None, "inventory_long_limit"
        elif inventory <= -self.max_inventory:
            ask, reason = None, "inventory_short_limit"

        # Rounding, skew, or a one-tick market can invert the pair or
        # push a side outside the book. A quote that crosses the touch is
        # a TAKER order wearing a maker's clothes — it would pay the
        # spread instead of earning it, and pay the 7% taker fee too.
        if bid is not None and bid.price >= best_ask.price:
            bid, reason = None, "bid_would_cross"
        if ask is not None and ask.price <= best_bid.price:
            ask, reason = None, "ask_would_cross"
        if bid is not None and ask is not None and bid.price >= ask.price:
            bid = ask = None
            reason = "quotes_inverted_after_rounding"
        if bid is not None and bid.price <= 0.0:
            bid, reason = None, "bid_below_tick"
        if ask is not None and ask.price >= 1.0:
            ask, reason = None, "ask_above_one"

        return QuotePair(**base, bid=bid, ask=ask, reason=reason, metadata=meta)
