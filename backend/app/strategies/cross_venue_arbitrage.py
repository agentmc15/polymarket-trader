"""Cross-venue complement arbitrage (PLAN.md D8, T18).

THE TRADE
---------
Buy YES on venue A and NO on venue B (or NO on A and YES on B), hold BOTH
to resolution. If — and only if — the two contracts pay out on the SAME
underlying fact, exactly one of them redeems for $1.00 and the pair is
riskless: profit is `1.00 - (what the pair cost)`. "Buy on the cheap
venue, sell on the dear one" is NOT available here; neither venue
supports a naked short, so the only riskless shape is buy-both-and-hold
(PLAN.md D8).

Capital is committed on BOTH venues at once and stays committed until
resolution. It is never moved between them: Kalshi dollars sit in a
CFTC-regulated FCM account whose ACH/wire settlement is measured in DAYS
(PLAN.md §3, `Settings.transfer_latency_hours` = 72 by default) while
Polymarket dollars are USDC on Polygon. **No transfer between venues is
ever assumed anywhere in this module** — there is no code path here that
moves, nets, or sums capital across venues, and `calculate_position_size`
below deliberately refuses the pooled `portfolio_value` scalar its own
base-class signature hands it (GUARDRAILS.md §1.6, PLAN.md R6). The only
cross-venue capital operation this strategy knows is `min`.

THE ARITHMETIC, WRITTEN OUT SO NOBODY "SIMPLIFIES" IT BACK
-----------------------------------------------------------
A naive engine computes `1 - (ask_A + ask_B + fees)` and calls the
remainder profit. **That number is only true if the two contracts
actually settle together.** `app/services/matching/` and
`app/models/event_link.py` exist precisely because they often do not:
Kalshi's *"Bitcoin above 100K by Dec 31"* and Polymarket's equivalent
scored title Jaccard 1.0 and confidence 0.90 while resolving against the
CoinDesk BPI at **5pm ET versus 4pm ET** — one hour apart on a volatile
asset. When the equivalence fails you do not merely miss the edge: you
hold YES on one venue and NO on the other, one of them pays zero, and
**you lose the losing leg's entire cost.** So::

    cost        = ask_A + ask_B + fee_A(taker) + fee_B(taker)
                  + 2 * redemption_gas   (all PER CONTRACT — see below)
    gross_edge  = 1 - cost
    worst_case_loss   = max(ask_A, ask_B)
    p_same_resolution = link.confidence          (never a `proposed` link)
    net_edge    = gross_edge * p_same_resolution
                  - (1 - p_same_resolution) * worst_case_loss

`net_edge` is the honest number and the only one this strategy gates on.
Worked example (the numbers `tests/strategies/test_cross_venue.py`
asserts to 1e-9), Polymarket YES ask 0.46 in Politics (published category
taker rate 0.04) against Kalshi NO ask 0.50 (`Settings` taker rate 0.07),
`probe_size = 50`::

    fee_A = 50 * 0.04 * 0.46 * 0.54 = 0.4968     -> 0.009936 / contract
    fee_B = ceil_cents(50 * 0.07 * 0.50 * 0.50)
          = ceil_cents(0.875) = 0.88             -> 0.017600 / contract
    gas   = 2 * 0.05 / 50                        =  0.002000 / contract
    cost  = 0.46 + 0.50 + 0.009936 + 0.0176 + 0.002 = 0.989536
    gross_edge = 1 - 0.989536 = 0.010464

    at p_same_resolution = 1.00 (a certainty no real link earns):
        net_edge = 0.010464 * 1.0 - 0.0 * 0.50 = 0.010464
    at p_same_resolution = 0.80:
        worst_case_loss = max(0.46, 0.50) = 0.50
        net_edge = 0.010464 * 0.8 - 0.20 * 0.50
                 = 0.0083712 - 0.10 = -0.0916288

Read that second line again. A 20% chance the two markets settle
differently costs 10 cents a contract in expectation, which swamps a
1-cent gross edge by an order of magnitude. At `confidence = 0.8` the
gross edge would have to exceed 12.5% before the trade is worth taking at
all. **That is the point of this module.** Deleting the second term does
not simplify the formula; it deletes the risk.

WHY A LARGE EDGE IS EVIDENCE AGAINST THE LINK (R1)
--------------------------------------------------
`net_edge >= SUSPECT_LINK_NET_EDGE` (8%) stamps
`metadata["suspect_link"] = True`. This is NOT a "great opportunity"
marker — it is the inverse. Genuine cross-venue mispricings of 8%+ on
liquid, linked markets do not persist; two venues quoting complementary
outcomes that far apart are far more likely quoting two DIFFERENT
questions. PLAN.md R1: "if the opportunity scanner reports a cross-venue
edge > 8% net, treat it as a probable link error first and surface the
two rules texts." The flag exists so T19's scanner can do exactly that.
A flagged intent is a request for a human to re-read both `rules_text`s,
not a signal to size up.

PER-CONTRACT UNITS, AND WHY THE FIXED GAS IS AMORTIZED
------------------------------------------------------
Every term above is PER CONTRACT, because the thing it is compared
against — the $1.00 a winning contract redeems for — is per contract.
Two of the inputs are not naturally per-contract and are converted here:

* **Fees.** Both venues charge `size * rate * price * (1 - price)`, but
  Kalshi additionally ceilings each FILL to whole cents for non-direct
  members (`app/venues/fees.py`). That floor is a fixed $0.01 minimum per
  fill, so its per-contract weight depends entirely on how many contracts
  the fill carries. Measured on this repo's own fee models: at `p = 0.995`
  a 1-contract fill costs 0.025% of notional on Polymarket and 1.005% on
  Kalshi — a 40x difference that exists only because of the cent floor,
  while Polymarket's `p(1-p)` term collapses at the tails. This module
  therefore prices each leg's fee for the WHOLE `probe_size` fill and
  divides, rather than calling `fee(price, 1.0, ...)` — which would
  charge the cent floor once per contract and overstate Kalshi's cost by
  ~14% at these prices. **That is why `probe_size` defaults to 50 and not
  to 1**: it is simultaneously the depth the book is walked for and the
  fill the cent floor is amortized over, and the two must be the same
  number or the quoted ask and the quoted fee describe different trades.
* **Redemption gas.** `settings.redemption_gas_usd` is a Polygon cost
  charged PER POSITION redeemed, not per contract, and there are two
  positions (one per leg). It is amortized over `probe_size` exactly as
  `binary_complement_arbitrage` amortizes it over `min_position_size`,
  for the same reason and with the same conservatism: at any fill LARGER
  than `probe_size` the real per-contract gas is smaller, never larger.

GUARDRAILS.md §1.5: no fee is ever a literal here. A cross-venue intent
needs TWO schedules — one per leg's own venue, unlike the same-venue
complement strategy's single one. Polymarket's comes from
`category_fee_schedule()` (which stamps `source="settings_override"` when
an operator override supplied the rate, so an operator-zeroed rate is
flagged downstream rather than trusted); Kalshi's from
`default_kalshi_schedule()`.

WHAT THIS STRATEGY REFUSES TO DO
--------------------------------
* It trades ONLY `status == "approved"` links. `LinkBook` raises on
  anything else, including the `None` status an unsaved row carries
  (PLAN.md D9: nothing trades on a `proposed` link).
* It prices ONLY from a walked `OrderBook` covering the full
  `probe_size`. A top-of-book quote is not evidence that `probe_size`
  contracts are buyable at that price (PLAN.md R4), and a cross-venue arb
  priced off the touch is exactly the wishful thinking this module exists
  to refuse.
* It mints no ids. `client_order_id`/`trade_id` are globally unique
  across paper AND live (PLAN.md D4); a deterministic id derived from a
  link id could let a paper row block a live one, so `link_id` appears in
  `metadata` only and the router mints UUID4s.
"""
import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.config import settings
from app.models.event_link import EventLink
from app.strategies.base import (
    DOWNSIZE_TO_CAPITAL_KEY,
    BaseStrategy,
    Intent,
    Leg,
    MarketSnapshot,
    Signal,
    SignalType,
    normalize_outcome,
)
from app.utils.time import utcnow
from app.venues.base import FeeModel
from app.venues.fees import (
    KalshiFeeModel,
    PolymarketFeeModel,
    category_fee_schedule,
    default_kalshi_schedule,
)
from app.venues.types import FeeSchedule, OrderBook, VenueId

logger = logging.getLogger(__name__)

#: The only `EventLink.status` this strategy will trade. Spelled out
#: rather than imported as a `Literal` member so the comparison is a
#: plain string equality that reads the same in a traceback.
APPROVED = "approved"

#: Hours in a 365-day year — the annualization constant PLAN.md D10
#: defines `annualized_return = net_edge / max(hours, min_hours) * 8760`
#: against. Not a fee, not a rate: a unit conversion.
HOURS_PER_YEAR = 8760.0

#: `net_edge` at or above which `metadata["suspect_link"]` is set (PLAN.md
#: R1). A RISK tripwire, not an opportunity threshold — see the module
#: docstring. Overridable through `config["suspect_link_net_edge"]` so an
#: operator can tighten it; loosening it past ~0.10 defeats its purpose.
SUSPECT_LINK_NET_EDGE = 0.08

#: Key under which `calculate_position_size` expects the PER-VENUE
#: capital snapshot inside its `positions` argument, i.e.
#: `positions["__ledger__"] == ledger.available_by_venue()` ->
#: `{"polymarket": 5000.0, "kalshi": 50.0}`. Dunder-ish on purpose: the
#: rest of `positions` is keyed by `f"{venue}:{market_id}:{outcome}"`
#: position ids, and this key must never collide with one.
LEDGER_KEY = "__ledger__"

#: Below this the two float asks are treated as the same price.
_EPSILON = 1e-12

DEFAULT_CONFIG: dict[str, Any] = {
    # CONTRACTS. The depth each leg's book is walked for to obtain the
    # marginal ask, AND the fill size each leg's fee is priced at before
    # being divided back to a per-contract number, AND the divisor that
    # amortizes the fixed `2 * redemption_gas_usd`. These MUST be one
    # number (see the module docstring): a marginal ask quoted for 50
    # contracts and a fee quoted for 1 describe different trades. 50 is
    # large enough that Kalshi's whole-cent per-fill floor is amortized
    # rather than dominant, and small enough to be plausibly available at
    # the touch on a linked pair.
    "probe_size": 50.0,
    # Minimum `net_edge` (per contract, AFTER the resolution-mismatch
    # haircut) required to emit an intent. 0.015 = 1.5 cents a contract.
    "min_net_edge": 0.015,
    # Hard cap on contracts per leg, before the per-venue capital bound.
    "max_contracts": 1000.0,
    # See `SUSPECT_LINK_NET_EDGE`.
    "suspect_link_net_edge": SUSPECT_LINK_NET_EDGE,
}


def _fee_inputs(venue: VenueId, category: str | None) -> tuple[FeeModel, FeeSchedule]:
    """Return the `(FeeModel, FeeSchedule)` that prices a taker fill on `venue`.

    Never a literal fee rate (GUARDRAILS.md §1.5). A cross-venue intent
    calls this ONCE PER LEG with that leg's OWN venue — unlike
    `binary_complement_arbitrage`, whose two legs share one venue and
    therefore one schedule. Polymarket's rate comes from
    `category_fee_schedule()`, which stamps `source="settings_override"`
    when `settings.polymarket_taker_fee_overrides` supplied it (so an
    operator-zeroed rate is not mistaken downstream for Polymarket's own
    published zero); Kalshi's comes from `Settings` via
    `default_kalshi_schedule()`.

    Args:
        venue: `"polymarket"` or `"kalshi"`.
        category: The market's category (Polymarket only; Kalshi's rate
            is not category-dependent and this is ignored there).

    Returns:
        tuple[FeeModel, FeeSchedule]: The model to call `.fee()` on and
            the schedule to pass it.
    """
    if venue == "kalshi":
        return KalshiFeeModel(), default_kalshi_schedule()
    return PolymarketFeeModel(), category_fee_schedule(category)


def _complement(outcome: str) -> str | None:
    """Return the other side of a binary outcome, or `None` if not binary.

    Args:
        outcome: An outcome name, already canonical
            (`app.strategies.base.normalize_outcome`).

    Returns:
        str | None: `"NO"` for `"YES"` and vice versa; `None` for any
            other name (a multi-outcome market has no single complement,
            and this strategy does not trade one).
    """
    if outcome == "YES":
        return "NO"
    if outcome == "NO":
        return "YES"
    return None


class LinkBook:
    """The approved-only set of `EventLink`s this strategy may trade.

    Constructing one is the enforcement point for PLAN.md D9's rule that
    *nothing trades on a `proposed` link*. The caller is expected to have
    already filtered on `status == "approved"`; this asserts it rather
    than trusting it, because the cost of being wrong is a pair of naked
    directional bets dressed up as an arbitrage (PLAN.md R1). An UNSAVED
    `EventLink` carries `status is None` (the `"proposed"` default is a
    COLUMN default applied at INSERT, not an attribute default), so a row
    that never reached the database is refused here too.

    A link whose `outcome_map` is empty is accepted into the book but is
    not tradeable: D9 makes filling it in a human's job at approval time,
    and `CrossVenueArbitrageStrategy` skips it with a warning rather than
    guessing a mapping. Keeping it in the book means one unmapped row
    does not hide every other link on the same market.
    """

    def __init__(self, links: Iterable[EventLink] = ()) -> None:
        """Index `links` by both of their `(venue, market)` endpoints.

        Args:
            links: Approved `EventLink` rows. May be empty.

        Raises:
            ValueError: If any link's `status` is not exactly
                `"approved"`.
        """
        self._links: tuple[EventLink, ...] = tuple(links)
        self._by_market: dict[tuple[str, str], list[EventLink]] = {}
        for link in self._links:
            if link.status != APPROVED:
                raise ValueError(
                    f"cross_venue_arbitrage trades approved links only, got "
                    f"status={link.status!r} for "
                    f"({link.venue_a}:{link.market_a} <-> "
                    f"{link.venue_b}:{link.market_b}); PLAN.md D9 — nothing "
                    "trades on a proposed link"
                )
            self._by_market.setdefault((link.venue_a, link.market_a), []).append(link)
            self._by_market.setdefault((link.venue_b, link.market_b), []).append(link)

    def __len__(self) -> int:
        """Return how many links this book holds."""
        return len(self._links)

    @property
    def links(self) -> tuple[EventLink, ...]:
        """Return every link in this book, in the order supplied."""
        return self._links

    def for_market(self, venue: str, market_id: str) -> tuple[EventLink, ...]:
        """Return every link touching `(venue, market_id)` on either side.

        Args:
            venue: `"polymarket"` or `"kalshi"`.
            market_id: Venue-native market identifier.

        Returns:
            tuple[EventLink, ...]: Possibly empty.
        """
        return tuple(self._by_market.get((venue, market_id), ()))


@dataclass(frozen=True)
class CrossVenueEvaluation:
    """One priced direction of one link — the whole D8 computation.

    Immutable and free of any sizing: every field here is PER CONTRACT
    (or a pure ratio), so it is meaningful before anyone knows how much
    capital is available. `CrossVenueArbitrageStrategy.on_market_data`
    sizes afterwards and folds these into `Intent.metadata`.

    Attributes:
        link_id: `EventLink.id`, or `None` for an unsaved row. Carried in
            metadata for review; NEVER used to build an order id (PLAN.md
            D4 — `client_order_id`/`trade_id` are globally unique across
            paper and live).
        venue_a: Venue of the first leg.
        market_a: Market id of the first leg.
        outcome_a: Canonical outcome bought on `venue_a`.
        ask_a: Size-weighted ask for `probe_size` contracts on leg A, a
            probability in [0, 1].
        fee_a: Leg A's taker fee, USD PER CONTRACT.
        venue_b: Venue of the second leg.
        market_b: Market id of the second leg.
        outcome_b: Canonical outcome bought on `venue_b` — always the
            complement of what `outcome_a` maps to under the link.
        ask_b: Size-weighted ask for leg B.
        fee_b: Leg B's taker fee, USD PER CONTRACT.
        gas_per_contract: `2 * settings.redemption_gas_usd / probe_size`.
        cost: `ask_a + ask_b + fee_a + fee_b + gas_per_contract`, USD per
            contract.
        gross_edge: `1 - cost`. What a NAIVE engine would call profit.
        p_same_resolution: `link.confidence`. The probability the two
            contracts settle on the same fact.
        worst_case_loss: `max(ask_a, ask_b)` — the losing leg's cost,
            which is what is lost outright when they do not.
        net_edge: `gross_edge * p - (1 - p) * worst_case_loss`. The only
            number this strategy gates on.
        hours_to_resolution: Hours from now to the EARLIER of the two
            close times. The earlier one governs: capital is locked until
            both legs are done, but the edge is only real while both are
            live.
        annualized: `net_edge / max(hours, min_hours) * HOURS_PER_YEAR`.
        expected_resolution_ts: The earlier of the two close times.
        probe_size: The contract count `ask_*`/`fee_*` were priced at.
        suspect_link: `net_edge >= config["suspect_link_net_edge"]`. See
            the module docstring: this is evidence AGAINST the link.
        fee_schedule_source_a: Provenance of leg A's fee rate.
        fee_schedule_source_b: Provenance of leg B's fee rate.
    """

    link_id: int | None
    venue_a: VenueId
    market_a: str
    outcome_a: str
    ask_a: float
    fee_a: float
    venue_b: VenueId
    market_b: str
    outcome_b: str
    ask_b: float
    fee_b: float
    gas_per_contract: float
    cost: float
    gross_edge: float
    p_same_resolution: float
    worst_case_loss: float
    net_edge: float
    hours_to_resolution: float
    annualized: float
    expected_resolution_ts: datetime
    probe_size: float
    suspect_link: bool
    fee_schedule_source_a: str
    fee_schedule_source_b: str

    def ask_by_venue(self) -> dict[str, float]:
        """Return `{venue: marginal ask}` for this evaluation's two legs.

        The shape `calculate_position_size` divides each venue's OWN free
        balance by. A mapping, never a scalar, for the same reason
        `CapitalLedger.available_by_venue()` is one: there is no such
        thing as "the" ask of a cross-venue pair.

        Returns:
            dict[str, float]: Two entries — the two legs are always on
                different venues (`Intent`'s `cross_venue` rule).
        """
        return {self.venue_a: self.ask_a, self.venue_b: self.ask_b}

    def as_metadata(self) -> dict[str, Any]:
        """Return the `Intent.metadata` payload for this evaluation.

        Returns:
            dict[str, Any]: Every field a reviewer or T19's scorer needs
                to re-derive `net_edge` by hand, plus the R1 tripwire.
        """
        return {
            "strategy": CrossVenueArbitrageStrategy.name,
            "link_id": self.link_id,
            "ask_a": self.ask_a,
            "ask_b": self.ask_b,
            "fee_a": self.fee_a,
            "fee_b": self.fee_b,
            "gas_per_contract": self.gas_per_contract,
            "cost": self.cost,
            "gross_edge": self.gross_edge,
            "p_same_resolution": self.p_same_resolution,
            "worst_case_loss": self.worst_case_loss,
            "net_edge": self.net_edge,
            "hours_to_resolution": self.hours_to_resolution,
            "annualized": self.annualized,
            "probe_size": self.probe_size,
            "suspect_link": self.suspect_link,
            "fee_schedule_source_a": self.fee_schedule_source_a,
            "fee_schedule_source_b": self.fee_schedule_source_b,
            "is_arbitrage": True,
        }


class CrossVenueArbitrageStrategy(BaseStrategy):
    """Buy YES on one venue and NO on the other, hold both to resolution.

    Holds a `LinkBook` of APPROVED `EventLink`s and a cache of the latest
    `OrderBook` per `(venue, market_id, outcome)` plus the latest
    `MarketSnapshot` per `(venue, market_id)`. Each `on_market_data` call
    updates those caches and then, for every approved link touching the
    market that just ticked, prices BOTH directions (`YES@A + NO@B` and
    `NO@A + YES@B`) and returns the best intent that clears both gates.

    Capital: call `observe_capital(ledger.available_by_venue())` before
    `on_market_data` to have sizing bounded by the poorer venue. Without
    it the strategy sizes at `config["max_contracts"]` and leaves the
    capital bound to `OrderRouter`, which enforces the same `min` at
    reserve time against the authoritative ledger. Neither path ever adds
    one venue's balance to the other's.
    """

    name = "cross_venue_arbitrage"
    description = "Buy YES on venue A + NO on venue B across an approved link"
    version = "1.0.0"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        links: Sequence[EventLink] | None = None,
    ) -> None:
        """Initialize with merged config and an approved-only link book.

        Args:
            config: Overrides for `DEFAULT_CONFIG`.
            links: Approved `EventLink` rows. Keyword-only and optional
                so `get_strategy("cross_venue_arbitrage")` still works;
                a strategy with no links simply never signals. Callers
                that load links later use `set_links()`.

        Raises:
            ValueError: If any link's `status` is not `"approved"`.
        """
        merged_config = {**DEFAULT_CONFIG, **(config or {})}
        super().__init__(merged_config)
        self._links = LinkBook(links or ())
        self._books: dict[tuple[str, str, str], OrderBook] = {}
        self._snapshots: dict[tuple[str, str], MarketSnapshot] = {}
        self._available: dict[str, float] | None = None
        self._opportunities_found = 0
        self._suspect_links_seen = 0

    # -- injection points -------------------------------------------------

    def set_links(self, links: Sequence[EventLink]) -> None:
        """Replace the link book.

        Args:
            links: Approved `EventLink` rows.

        Raises:
            ValueError: If any link's `status` is not `"approved"`.
        """
        self._links = LinkBook(links)

    @property
    def links(self) -> LinkBook:
        """Return the approved-only `LinkBook` this strategy trades."""
        return self._links

    def observe_capital(self, available: Mapping[str, float]) -> None:
        """Record the PER-VENUE free-capital snapshot sizing is bounded by.

        The argument is `CapitalLedger.available_by_venue()` verbatim —
        `{"polymarket": 5000.0, "kalshi": 50.0}`. It is stored as a
        mapping and consumed only through `min(...)`; nothing here or in
        `calculate_position_size` ever adds two venues' balances
        (GUARDRAILS.md §1.6). Passing a partial mapping is safe: a venue
        absent from it is treated as unfundable (0 contracts), never as
        "fund it from the other one".

        Args:
            available: Venue -> free USD.
        """
        self._available = dict(available)

    def observe_book(self, book: OrderBook) -> None:
        """Cache one venue's book for one `(market, outcome)`.

        A `MarketSnapshot` carries at most ONE outcome's book, but this
        strategy needs the ask on BOTH sides of BOTH markets, so a caller
        with more books than the ticking snapshot carries (T19's scanner,
        which calls `get_book` per outcome) feeds them in here.

        Args:
            book: A normalized `OrderBook`. Its `outcome` is canonicalized
                through `app.strategies.base.normalize_outcome` before
                being used as a cache key, so Gamma's `"Yes"` and
                Kalshi's `"YES"` land on the same entry.
        """
        self._books[(book.venue, book.market_id, normalize_outcome(book.outcome))] = book

    # -- the strategy hook ------------------------------------------------

    def on_market_data(self, snapshot: MarketSnapshot) -> Intent | None:
        """Price every approved link touching this market and return the best.

        Args:
            snapshot: The market that just ticked. Its `book`, if
                present, is cached; its `end_date` and `category` are
                cached for the leg on that venue.

        Returns:
            Intent | None: A 2-leg `kind="cross_venue"` `Intent` (both
                BUY, held to resolution, `atomicity="all_or_none"`) for
                the highest-`net_edge` direction that clears BOTH
                `config["min_net_edge"]` and
                `settings.min_viable_annualized`; `None` if nothing does.
        """
        self._snapshots[(snapshot.venue, snapshot.market_id)] = snapshot
        if snapshot.book is not None:
            self.observe_book(snapshot.book)

        best: CrossVenueEvaluation | None = None
        for link in self._links.for_market(snapshot.venue, snapshot.market_id):
            for outcome_a in ("YES", "NO"):
                evaluation = self.evaluate(link, outcome_a)
                if evaluation is None:
                    continue
                if evaluation.net_edge < float(self.config["min_net_edge"]):
                    continue
                if evaluation.annualized < settings.min_viable_annualized:
                    continue
                if best is None or evaluation.net_edge > best.net_edge:
                    best = evaluation

        if best is None:
            return None
        return self._build_intent(best)

    def evaluate(
        self, link: EventLink, outcome_a: str
    ) -> CrossVenueEvaluation | None:
        """Price ONE direction of ONE link, per PLAN.md D8.

        Public because the arithmetic is the product: a test (and T19's
        scanner) must be able to assert `cost`/`gross_edge`/`net_edge`
        without going through the emission gates.

        Both legs are priced by walking their own cached `OrderBook` for
        `config["probe_size"]` contracts. A leg with no cached book, or
        one whose book cannot supply the full probe size, returns `None`:
        a price the book cannot actually fill is not a price (PLAN.md R4).

        Args:
            link: An APPROVED `EventLink` (`LinkBook` already enforced
                that; `link.confidence` is used verbatim as
                `p_same_resolution`).
            outcome_a: `"YES"` or `"NO"` — the outcome BOUGHT on
                `link.venue_a`. The leg on `venue_b` is then the
                COMPLEMENT of whatever `link.outcome_map` maps this to,
                so that exactly one of the two legs pays $1.00 when the
                link holds.

        Returns:
            CrossVenueEvaluation | None: `None` when the direction cannot
                be priced at all — no outcome map, a non-binary mapping,
                a missing or too-thin book on either leg, or a missing
                close time on either side.
        """
        outcome_a = normalize_outcome(outcome_a)
        mapped = link.outcome_map.get(outcome_a) if link.outcome_map else None
        if mapped is None:
            logger.debug(
                "cross_venue",
                extra={
                    "event": "no_outcome_map",
                    "link_id": link.id,
                    "outcome_a": outcome_a,
                },
            )
            return None
        outcome_b = _complement(normalize_outcome(str(mapped)))
        if outcome_b is None or {outcome_a, outcome_b} != {"YES", "NO"}:
            # `Intent`'s `cross_venue` rule requires the two legs to be
            # one YES and one NO. An INVERTED outcome map (A's YES
            # corresponds to B's NO) would need YES on both venues to
            # hedge, which that rule cannot express — so the direction is
            # skipped rather than silently mis-hedged. The matcher only
            # ever writes `{"YES": "YES", "NO": "NO"}` or `{}`
            # (`app/services/matching/matcher.py`), so this is reachable
            # only from a human-supplied map.
            logger.warning(
                "cross_venue",
                extra={
                    "event": "unrepresentable_outcome_pair",
                    "link_id": link.id,
                    "outcome_a": outcome_a,
                    "outcome_b": outcome_b,
                },
            )
            return None

        venue_a: VenueId = link.venue_a  # type: ignore[assignment]
        venue_b: VenueId = link.venue_b  # type: ignore[assignment]
        snapshot_a = self._snapshots.get((venue_a, link.market_a))
        snapshot_b = self._snapshots.get((venue_b, link.market_b))
        if snapshot_a is None or snapshot_b is None:
            return None
        if snapshot_a.end_date is None or snapshot_b.end_date is None:
            # No close time means no `hours_to_resolution`, which means no
            # annualized return, which means the D10 gate cannot be
            # evaluated. Never invented.
            return None

        size = float(self.config["probe_size"])
        ask_a = self._marginal_ask(venue_a, link.market_a, outcome_a, size)
        ask_b = self._marginal_ask(venue_b, link.market_b, outcome_b, size)
        if ask_a is None or ask_b is None:
            return None

        model_a, schedule_a = _fee_inputs(venue_a, snapshot_a.category)
        model_b, schedule_b = _fee_inputs(venue_b, snapshot_b.category)
        # Priced as ONE aggregate fill of the whole probe and divided
        # back (module docstring) -- `ask_a`/`ask_b` are `_marginal_ask`'s
        # size-weighted blend across however many book levels `walk`
        # actually consumed, and this call charges the fee for that
        # blended price once, as if it were a single fill. It is NOT "the
        # fill it is actually charged on": on Kalshi the whole-cent
        # per-fill floor is charged PER FILL, so a probe that walks
        # multiple levels is charged that floor multiple times in
        # reality, once here. The error is one-directional -- this always
        # UNDERSTATES Kalshi's true cost when the walk spans more than
        # one level, never overstates it -- and measured on a fragmented
        # tail book it has understated edge by up to 0.86 percentage
        # points, against this strategy's own `min_net_edge` gate of
        # 0.015 (1.5%). Left as a known approximation (not fixed here):
        # unifying the fee basis across all four scored strategies is a
        # separate change with its own blast radius.
        fee_a = model_a.fee(ask_a, size, "taker", schedule_a) / size
        fee_b = model_b.fee(ask_b, size, "taker", schedule_b) / size
        gas_per_contract = (2.0 * settings.redemption_gas_usd) / size

        cost = ask_a + ask_b + fee_a + fee_b + gas_per_contract
        gross_edge = 1.0 - cost
        p_same = float(link.confidence)
        worst_case_loss = max(ask_a, ask_b)
        net_edge = gross_edge * p_same - (1.0 - p_same) * worst_case_loss

        resolution_ts = min(snapshot_a.end_date, snapshot_b.end_date)
        hours = (resolution_ts - utcnow()).total_seconds() / 3600.0
        annualized = (
            net_edge / max(hours, settings.min_hours_for_annualization)
        ) * HOURS_PER_YEAR

        suspect = net_edge >= float(self.config["suspect_link_net_edge"])
        if suspect:
            self._suspect_links_seen += 1
            logger.warning(
                "cross_venue",
                extra={
                    "event": "suspect_link",
                    "link_id": link.id,
                    "net_edge": net_edge,
                    "venue_a": venue_a,
                    "market_a": link.market_a,
                    "venue_b": venue_b,
                    "market_b": link.market_b,
                    "note": (
                        "an 8%+ cross-venue net edge is evidence the link is "
                        "WRONG, not that the trade is good (PLAN.md R1) — "
                        "re-read both rules_text before routing"
                    ),
                },
            )

        return CrossVenueEvaluation(
            link_id=link.id,
            venue_a=venue_a,
            market_a=link.market_a,
            outcome_a=outcome_a,
            ask_a=ask_a,
            fee_a=fee_a,
            venue_b=venue_b,
            market_b=link.market_b,
            outcome_b=outcome_b,
            ask_b=ask_b,
            fee_b=fee_b,
            gas_per_contract=gas_per_contract,
            cost=cost,
            gross_edge=gross_edge,
            p_same_resolution=p_same,
            worst_case_loss=worst_case_loss,
            net_edge=net_edge,
            hours_to_resolution=hours,
            annualized=annualized,
            expected_resolution_ts=resolution_ts,
            probe_size=size,
            suspect_link=suspect,
            fee_schedule_source_a=schedule_a.source,
            fee_schedule_source_b=schedule_b.source,
        )

    # -- sizing -----------------------------------------------------------

    def calculate_position_size(
        self,
        signal: Signal,
        portfolio_value: float,  # noqa: ARG002 - REFUSED on purpose, see below
        positions: dict[str, Any],
    ) -> float:
        """Return CONTRACTS per leg, bounded by the POORER venue (PLAN.md D8).

        Two deliberate departures from `BaseStrategy.calculate_position_size`,
        both forced by the same fact — capital is not fungible across
        venues (GUARDRAILS.md §1.6, PLAN.md R6):

        1. **This returns CONTRACTS, not dollars.** A cross-venue
           complement's two legs must be equal in CONTRACTS; equal
           DOLLARS on unequal-priced legs leaves a naked directional
           residual on the cheaper side, which is the opposite of the
           hedge.
        2. **`portfolio_value` is ignored, on purpose.** It is a single
           scalar spanning both venues — precisely the cross-venue sum
           §1.6 forbids as a sizing input. Sizing reads
           `positions["__ledger__"]` instead, which is
           `CapitalLedger.available_by_venue()` and cannot be collapsed
           to a total without someone writing the sum out by hand.

        The bound is `min(available_A / ask_A, available_B / ask_B,
        max_contracts)`. Note what it is NOT: it is not
        `(available_A + available_B) / (ask_A + ask_B)`. With $5,000 on
        Polymarket and $50 on Kalshi the second formula funds ~5,200
        contracts; the first funds ~100, because the $5,000 cannot reach
        the Kalshi leg inside this trade. No transfer between venues is
        assumed, requested, or possible here — an ACH settlement measured
        in days is not capital available to an order being placed now.

        Args:
            signal: The signal being sized. `signal.metadata["leg_asks"]`
                must be `{venue: marginal ask}` for the intent's two legs
                (`CrossVenueEvaluation.ask_by_venue()`); without it this
                falls back to `signal.price` for the signal's own venue
                only, which can only size a one-venue trade.
            portfolio_value: IGNORED — see above. Present only because
                `BaseStrategy` declares it.
            positions: Current positions, plus `positions["__ledger__"]`
                = `{venue: free USD}`. When that key is ABSENT the
                per-venue bound is unknown, and this returns
                `max_contracts` rather than inventing a budget from
                `portfolio_value`: `OrderRouter` re-applies the same
                per-venue `min` at reserve time against the authoritative
                ledger and downsizes there, so the bound is never lost —
                it is only applied later.

        Returns:
            float: Contracts per leg, `>= 0`.
        """
        max_contracts = float(self.config["max_contracts"])
        asks = signal.metadata.get("leg_asks")
        if not isinstance(asks, Mapping) or not asks:
            asks = {}
        if not asks:
            price = signal.price
            if price <= 0.0:
                return 0.0
            asks = {signal.metadata.get("venue", "polymarket"): price}

        ledger = positions.get(LEDGER_KEY)
        if not isinstance(ledger, Mapping):
            return max_contracts

        bounds = [max_contracts]
        for venue, ask in asks.items():
            if ask <= _EPSILON:
                # A free leg is not a leg; refuse rather than divide by ~0
                # and report an unbounded size.
                return 0.0
            # `ledger.get(venue, 0.0)`: a venue this ledger was never
            # seeded for funds NOTHING. It is never covered from the
            # venue that IS seeded.
            bounds.append(float(ledger.get(venue, 0.0)) / float(ask))
        return max(0.0, min(bounds))

    # -- internals --------------------------------------------------------

    def _marginal_ask(
        self, venue: str, market_id: str, outcome: str, size: float
    ) -> float | None:
        """Return the size-weighted ask for `size` contracts, or `None`.

        Uses `OrderBook.walk("buy", size)` — the repo's single depth
        primitive (best-first, never over-fills, order-independent). A
        book that runs dry before `size` returns `None`: the average of a
        partial fill is not the price of the whole order, and reporting
        it as one is how a paper arb survives a book that could never
        have filled it (PLAN.md R4).

        Args:
            venue: Venue the book was read from.
            market_id: Venue-native market identifier.
            outcome: Canonical outcome, `"YES"`/`"NO"`.
            size: Contracts to price, `> 0`.

        Returns:
            float | None: Size-weighted ask in [0, 1], or `None` if no
                book is cached for this key or it cannot supply `size`.
        """
        book = self._books.get((venue, market_id, outcome))
        if book is None:
            return None
        fills = book.walk("buy", size)
        if not fills:
            return None
        filled = math.fsum(qty for _, qty in fills)
        if filled < size - 1e-9:
            logger.debug(
                "cross_venue",
                extra={
                    "event": "thin_book",
                    "venue": venue,
                    "market_id": market_id,
                    "outcome": outcome,
                    "wanted": size,
                    "available": filled,
                },
            )
            return None
        return math.fsum(price * qty for price, qty in fills) / filled

    def _build_intent(self, evaluation: CrossVenueEvaluation) -> Intent | None:
        """Size `evaluation` against per-venue capital and build the `Intent`.

        Args:
            evaluation: The winning direction.

        Returns:
            Intent | None: The 2-leg cross-venue intent, or `None` if the
                per-venue capital bound sizes it to nothing or leaves it
                below `settings.min_trade_usd`.
        """
        sizing_signal = Signal(
            type=SignalType.BUY,
            market_id=evaluation.market_a,
            token_id=evaluation.market_a,
            outcome=evaluation.outcome_a,
            price=evaluation.ask_a,
            size=0.0,
            confidence=evaluation.p_same_resolution,
            metadata={
                "venue": evaluation.venue_a,
                "leg_asks": evaluation.ask_by_venue(),
            },
        )
        positions: dict[str, Any] = {}
        if self._available is not None:
            positions[LEDGER_KEY] = dict(self._available)
        contracts = self.calculate_position_size(sizing_signal, 0.0, positions)
        if contracts <= 0.0:
            return None

        capital_lockup_usd = contracts * (evaluation.ask_a + evaluation.ask_b)
        if capital_lockup_usd < settings.min_trade_usd:
            # Below the engine/router notional floor this would be
            # rejected downstream as dust; not emitting it is quieter and
            # equally honest.
            return None

        self._opportunities_found += 1
        metadata = evaluation.as_metadata()
        metadata["capital_lockup_usd"] = capital_lockup_usd
        # The two legs are equal in CONTRACTS and sit on two venues whose
        # balances never combine, so a shortfall on one must shrink BOTH
        # legs rather than reject the pair: this asks `OrderRouter` to
        # re-apply `min(available/ask)` against the live ledger at reserve
        # time and downsize to what the poorer venue can fund. Legal here
        # and not for a directional single-leg intent because this edge is
        # PER CONTRACT and therefore scale-free — a smaller pair is the
        # same trade, while a smaller directional bet is a different one.
        metadata[DOWNSIZE_TO_CAPITAL_KEY] = True

        return Intent(
            kind="cross_venue",
            legs=[
                Leg(
                    market_id=evaluation.market_a,
                    outcome=evaluation.outcome_a,
                    side="BUY",
                    limit_price=evaluation.ask_a,
                    size_contracts=contracts,
                    venue=evaluation.venue_a,
                ),
                Leg(
                    market_id=evaluation.market_b,
                    outcome=evaluation.outcome_b,
                    side="BUY",
                    limit_price=evaluation.ask_b,
                    size_contracts=contracts,
                    venue=evaluation.venue_b,
                ),
            ],
            hold_to_resolution=True,
            atomicity="all_or_none",
            confidence=evaluation.p_same_resolution,
            expected_resolution_ts=evaluation.expected_resolution_ts,
            metadata=metadata,
        )

    # -- bookkeeping ------------------------------------------------------

    def reset(self) -> None:
        """Reset caches and counters for a new run.

        The `LinkBook` is NOT cleared: which links a human approved is
        not run state.
        """
        super().reset()
        self._books.clear()
        self._snapshots.clear()
        self._available = None
        self._opportunities_found = 0
        self._suspect_links_seen = 0

    def get_stats(self) -> dict[str, Any]:
        """Get strategy statistics.

        Returns:
            dict[str, Any]: `BaseStrategy.get_stats()` plus how many
                intents were emitted and how many priced directions
                tripped the R1 suspect-link threshold.
        """
        stats = super().get_stats()
        stats.update(
            {
                "opportunities_found": self._opportunities_found,
                "suspect_links_seen": self._suspect_links_seen,
                "approved_links": len(self._links),
            }
        )
        return stats
