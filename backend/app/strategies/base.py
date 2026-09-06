"""Base strategy interface for backtesting and live trading."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from app.utils.time import ensure_aware, utcnow
from app.venues.types import OrderBook, OrderSide, VenueId

#: Shape of an `Intent` (PLAN.md D7):
#: - `"single"`: one leg, back-compat with a plain `Signal` (`Signal.to_intent()`).
#: - `"complement"`: 2 legs, same venue+market, outcomes `{"YES", "NO"}` —
#:   `binary_complement_arbitrage`.
#: - `"cross_venue"`: 2 legs on different venues — cross-venue complement arb
#:   (PLAN.md D8): YES on venue A + NO on venue B.
#: - `"bundle"`: >= 3 legs, same market, distinct outcomes —
#:   `multi_outcome_bundle_arbitrage`.
IntentKind = Literal["single", "complement", "bundle", "cross_venue"]

#: Execution semantics for an `Intent`'s legs: `"all_or_none"` means every leg
#: must fill or the whole intent is unwound; `"best_effort"` accepts partial
#: execution. T08 is where this is actually enforced; T06 only carries it.
AtomicityMode = Literal["all_or_none", "best_effort"]

#: `Intent.metadata` key by which a strategy declares that its intent may
#: be DOWNSIZED to what the poorer venue can fund, rather than rejected
#: outright, when a venue comes up short at reserve time
#: (`app.execution.router.OrderRouter._downsize_to_capital`).
#:
#: Opt-in, and it is the STRATEGY's call to make, because only the
#: strategy knows whether its edge is scale-free. A cross-venue or
#: same-venue complement held to resolution earns a fixed edge PER
#: CONTRACT, so a smaller pair is the same trade at a smaller size —
#: `min(available_A / ask_A, available_B / ask_B, max_contracts)` (PLAN.md
#: D8) is a BOUND on the size, and turning that bound into a refusal
#: throws away a trade the capital could actually support. A directional
#: single-leg bet is the opposite: the size was chosen deliberately
#: against a view, and silently placing a third of it is a different
#: trade, so it must still be rejected. The router therefore downsizes
#: only for intents carrying this key, and every leg is scaled to the SAME
#: contract count so the hedge is never left lopsided.
DOWNSIZE_TO_CAPITAL_KEY = "downsize_to_capital"

# --------------------------------------------------------------------------
# THE SCORING CONTRACT (T31). `Intent.metadata` keys that
# `app.services.scoring.score` reads to build one comparable `composite`
# out of every strategy's output.
#
# THE RULE, AND IT IS THE WHOLE POINT: a strategy publishes a PRE-RISK
# edge. `SCORING_EDGE_KEY` is USD per UNIT (one contract of every leg),
# net of trading fees and gas, and net of NOTHING ELSE — no probability
# haircut, no confidence multiplier, no discount for the chance that the
# trade's premise is wrong. Risk is priced by the scorer, in one place,
# exactly once.
#
# Before T31 that rule did not exist, and the four arbitrage strategies
# each meant something different by their published edge:
# `cross_venue_arbitrage` published a number ALREADY multiplied by its
# link confidence, while the three single-market strategies published a
# number that was not. `scoring` ranked all four against each other and
# then discounted the cross-venue one a SECOND time in
# `resolution_risk` — so the repo's centerpiece strategy was penalized
# twice for a risk the others were not penalized for at all, and the
# sort order on `GET /api/v1/arbitrage/opportunities` encoded that
# accounting artifact rather than the trades' merit.
#
# A strategy whose two legs might not settle on the same fact declares
# that risk with the two IDENTITY keys instead of folding it into its
# edge. The scorer applies `edge * p - (1 - p) * worst_case_loss` — the
# same formula `cross_venue_arbitrage` used internally — to any intent
# that publishes them, and leaves the edge alone for any intent that
# does not.
#
# T34: a strategy whose published `SCORING_EDGE_KEY` is not that
# per-unit, fee-netted, settlement-realized dollar figure at all — a
# directional mispricing bet, not an arbitrage edge — must not let the
# scorer find that out. It opts OUT via `EDGE_BASIS_DIRECTIONAL`, and
# `score()` refuses (`UnscorableIntent`) rather than scoring it. See
# that constant's docstring below.

#: Per-UNIT USD edge, net of fees and gas, BEFORE any risk haircut. The
#: one number `app.services.scoring` annualizes. "Unit" = one contract of
#: every leg (a complement pair, a bundle of N outcomes, a cross-venue
#: pair), which is how all four arbitrage strategies size: equal
#: contracts on every leg.
SCORING_EDGE_KEY = "edge"

#: Legacy alias for `SCORING_EDGE_KEY`, read only when it is absent.
#: `multi_outcome_bundle_arbitrage` published its (already pre-risk)
#: margin under this name before the contract was written down, and its
#: persisted rows still carry it. It means exactly the same thing:
#: pre-risk, per-unit, net of fees. `cross_venue_arbitrage` no longer
#: publishes this key at all — its post-haircut number rides under
#: `risk_adjusted_edge`, so no reader can mistake one for the other.
SCORING_EDGE_LEGACY_KEY = "net_edge"

#: Probability in `[0, 1]` that the intent's legs settle on the SAME
#: fact. Absent means 1.0 (a single-market intent: every leg resolves off
#: one question, so there is no identity risk to price). Present only
#: where the risk is real — `cross_venue_arbitrage` publishes its
#: `EventLink.confidence` here.
IDENTITY_CONFIDENCE_KEY = "p_same_resolution"

#: USD per unit lost when the legs DO NOT settle on the same fact. Not
#: the edge — the losing leg's entire stake. Read only alongside
#: `IDENTITY_CONFIDENCE_KEY`.
IDENTITY_WORST_CASE_LOSS_KEY = "worst_case_loss"

#: How the published edge was arrived at, so a human sorting by
#: `composite` can see which rows carry model risk and which do not.
#: Rides through `Intent.metadata` to `GET /arbitrage/opportunities`.
EDGE_BASIS_KEY = "edge_basis"

#: Every term in the edge is an observed price or a published fee/gas
#: rate. Arithmetic, no estimated parameter.
EDGE_BASIS_OBSERVED = "observed_costs"

#: The edge is observed costs MINUS an identity haircut whose
#: probability is an ESTIMATE (`app.services.matching`'s link
#: confidence), not a measured rate. Two rows with the same `composite`
#: are the same expected return only to the extent that estimate is
#: calibrated — see `app.services.scoring`'s "WHAT REMAINS
#: INCOMPARABLE".
EDGE_BASIS_IDENTITY_ESTIMATED = "identity_estimated"

#: T34 (NOTES.md). The published `SCORING_EDGE_KEY`/`SCORING_EDGE_LEGACY_KEY`
#: value, if any, is a DIRECTIONAL mispricing estimate — "I think this
#: price is wrong and will move" — not a fee-netted, settlement-realized,
#: per-contract USD edge. `favorite_compounder` and `no_bias_exploit`
#: both happen to spell their directional signal's metadata key `"edge"`
#: (the same string as `SCORING_EDGE_KEY`) for reasons that predate this
#: contract, but it is not the same NUMBER: it is `estimated_probability
#: - market_price`, a probability-space gap with no fee/gas netting and
#: no per-unit dollar meaning, and `app.services.scoring` has no formula
#: that turns it into a real annualized return.
#:
#: A strategy stamps this value on `EDGE_BASIS_KEY` to say, explicitly,
#: "whatever I published under `SCORING_EDGE_KEY` is not that number" —
#: `app.services.scoring._published_edge` refuses (`UnscorableIntent`)
#: any intent that declares it, rather than reading the bare `"edge"` key
#: and hoping it means the scoring contract's edge. This is what makes
#: the contract SURVIVE the day someone adds `favorite_compounder` or
#: `no_bias_exploit` to `STRATEGY_CATEGORIES["arbitrage"]` (a one-line,
#: innocuous-looking change): without this label the scorer would read
#: their directional gap as a riskless per-contract edge, annualize it,
#: and rank it against real arbitrage — with no error and nothing
#: visibly wrong. With it, routing either strategy's intent through
#: `score()` raises instead.
EDGE_BASIS_DIRECTIONAL = "directional_mispricing"

#: Canonical spelling for a binary market's two outcomes, keyed by their
#: case-folded form. `normalize_outcome()` below is the ONLY table this
#: maps through; every other outcome name (e.g. a multi-outcome bundle's
#: named outcome such as `"Trump"`) keeps the venue's own casing.
_CANONICAL_BINARY_OUTCOMES: dict[str, str] = {"yes": "YES", "no": "NO"}


def normalize_outcome(outcome: str) -> str:
    """Return the DISPLAY label for an outcome name.

    An outcome name has two jobs, and T21d (NOTES.md) split them because
    conflating them was a money bug:

    - a DISPLAY label — what the venue called this outcome, which belongs
      in the UI, in a `TradeRecord`, and in a log line. THIS function.
    - an IDENTITY — what two references to the same outcome must agree
      on so they land on the same dict key, position id, or database
      row. `outcome_key()` below, and nothing else.

    As a display label the only thing worth canonicalizing is the
    binary pair, because the two venues genuinely disagree about it:
    Polymarket's Gamma payload spells outcomes `"Yes"`/`"No"` verbatim
    (`app/venues/polymarket/adapter.py`) while Kalshi's adapter forces
    `"YES"`/`"NO"`, and every strategy in this kit hardcodes uppercase.
    A multi-outcome bundle's named outcome (`"Trump"`) is NOT a casing
    convention this function owns — a venue that capitalizes a candidate
    name one way is not making an error, and rewriting it would put a
    label on the screen that no venue ever printed.

    Surrounding whitespace is the one thing this DOES strip for every
    label, binary or not (T21d defect 5b): a leading or trailing space
    is never part of what a venue meant to call an outcome, it is a
    serialization artifact, and leaving it in place produced a `Leg`
    whose `"Trump "` was a different outcome from its own market's
    `"Trump"` — distinct enough to defeat `Intent`'s bundle
    distinct-outcome check and every identity built downstream.

    Args:
        outcome: Outcome name as supplied by a strategy or venue
            payload, e.g. `"Yes"`, `"yes"`, `"YES"`, or an arbitrary
            multi-outcome bundle name like `"Trump"`.

    Returns:
        str: `"YES"`/`"NO"` for any case-insensitive spelling of
            either; otherwise `outcome` with surrounding whitespace
            stripped and its casing untouched.
    """
    stripped = outcome.strip()
    return _CANONICAL_BINARY_OUTCOMES.get(stripped.casefold(), stripped)


def outcome_key(outcome: str) -> str:
    """Return the canonical IDENTITY for an outcome name.

    T21d (NOTES.md). This is the ONE canonicalization every site that
    KEYS on an outcome must use: `Backtester._current_prices`'s keys,
    `Position.position_id`, `Intent`'s bundle distinct-outcome check,
    `BookSnapshot.outcome` at persist, and
    `DataReplayer._get_recorded_book`'s lookup. Two references to the
    same outcome agree here or they are, silently, two different
    outcomes.

    WHY THIS IS NOT `normalize_outcome()`. Before T21d one function
    served both jobs, and it was case-insensitive for `"YES"`/`"NO"`
    only. That was defensible while binary markets were the only
    markets; T21 gave arbitrary outcome labels first-class depth
    (`BookSnapshot`) and first-class positions (a bundle leg), and the
    asymmetry became a money bug: everything that RESOLVED an outcome
    case-folded it, while everything that KEYED on one did not. A
    position `outcome="TRUMP"` therefore keyed
    `polymarket:M:TRUMP` while the payload's `"Trump"` keyed
    `polymarket:M:Trump`, so `Portfolio.total_equity` fell through to
    `pos.entry_price` and the position marked at its ENTRY PRICE
    forever — word for word the `"Yes"`-vs-`"YES"` failure Phase-1 FIX 1
    closed for binary markets, moved to the labels T21 added.

    WHY TWO FUNCTIONS RATHER THAN ONE. Making `normalize_outcome()`
    itself fold non-binary casing would fix the keys, but it is called
    at nine sites that put its result on a SCREEN or in a record
    (`Leg.outcome`, `Position.outcome`, `TradeRecord.outcome`,
    `links.py`'s reviewer-supplied outcome map, `scanner.py`'s book
    cache), and it would silently rewrite every venue's candidate name
    to lower case there too. Identity and display are different
    questions with different right answers, so they get different
    functions; `normalize_outcome()` keeps its promise, and every key
    goes through this one.

    The binary pair keeps its canonical `"YES"`/`"NO"` spelling as its
    identity rather than folding to `"yes"`/`"no"`, so every identity
    already written down (`"polymarket:M:YES"`, every `BookSnapshot`
    row, every test's expected position id) is byte-identical to what
    it was before T21d. Only non-binary labels change, and those had no
    working identity to preserve.

    Args:
        outcome: Outcome name in any spelling, e.g. `"Yes"`, `"TRUMP"`,
            `"Trump "`.

    Returns:
        str: `"YES"`/`"NO"` for the binary pair; otherwise the label
            stripped of surrounding whitespace and case-folded, so
            `"TRUMP"`, `"Trump"`, `"trump"` and `"Trump "` all key
            identically.
    """
    normalized = normalize_outcome(outcome)
    if normalized in _CANONICAL_BINARY_OUTCOMES.values():
        return normalized
    return normalized.casefold()


class SignalType(str, Enum):
    """Trading signal type."""

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class Signal:
    """Trading signal generated by a strategy.

    Attributes:
        type: Signal type (BUY, SELL, HOLD).
        market_id: Polymarket condition ID.
        token_id: Token ID for the specific outcome.
        outcome: Outcome name (e.g., "Yes", "No").
        price: Target execution price.
        size: Position size in dollars.
        confidence: Strategy confidence level (0.0 to 1.0).
        timestamp: When the signal was generated. Aware UTC, defaulting
            to `app.utils.time.utcnow()` — NOT `datetime.utcnow()`, which
            returns a naive value that would blow up the moment it met a
            tz-aware `PriceHistory.timestamp` or `MarketSnapshot`
            (GUARDRAILS.md §4, PLAN.md R9).
        stop_loss: Optional stop loss price.
        take_profit: Optional take profit price.
        metadata: Additional signal metadata.
    """

    type: SignalType
    market_id: str
    token_id: str
    outcome: str
    price: float
    size: float
    confidence: float
    timestamp: datetime = field(default_factory=utcnow)
    stop_loss: float | None = None
    take_profit: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate signal fields after initialization.

        Raises:
            TypeError: If `timestamp` is not a `datetime`.
            ValueError: If `timestamp` is naive, if `confidence` is
                outside `[0, 1]`, if `size` is negative, or if `price` is
                outside `[0, 1]`.
        """
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"Confidence must be between 0 and 1, got {self.confidence}")
        if self.size < 0:
            raise ValueError(f"Size must be non-negative, got {self.size}")
        if not 0.0 <= self.price <= 1.0:
            raise ValueError(f"Price must be between 0 and 1, got {self.price}")
        ensure_aware(self.timestamp)

    def to_intent(
        self,
        *,
        venue: VenueId = "polymarket",
        expected_resolution_ts: datetime | None = None,
    ) -> "Intent":
        """Convert this `Signal` into an equivalent one-leg `Intent` (PLAN.md D7).

        This is the back-compat path: every strategy already written
        against `Signal` keeps working unchanged, while the engine (and,
        from T08, `OrderRouter`) can normalize on `Intent` as the single
        execution unit. `size` (documented above as "Position size in
        dollars") becomes `Leg.size_usd`; `Leg.size_contracts` is left
        `None`.

        `Signal` itself carries no `venue`/resolution-time field to pull
        from, and this method has no snapshot to read one from either —
        so BOTH must be supplied by the caller that DOES have the
        triggering snapshot (Phase-1 remediation FIX 2 / FIX 4:
        `Backtester._process_snapshot` calls this as
        `result.to_intent(venue=snapshot.venue,
        expected_resolution_ts=snapshot.end_date)`). Defaulting `venue`
        to `"polymarket"` (matching `Leg.venue`'s own default) preserves
        every existing caller that constructs a `Signal` and converts it
        with no snapshot in hand at all (e.g. a unit test); a caller that
        DOES have a snapshot must pass its `venue` explicitly, or every
        leg silently becomes `"polymarket"` regardless of which venue's
        data produced the signal — the exact defect FIX 2 closes.

        Args:
            venue: The venue to stamp on the resulting `Leg`. See above.
            expected_resolution_ts: Stamped on the resulting `Intent`
                verbatim (must be aware UTC if not `None` — enforced by
                `Intent.__post_init__`). `None` when the caller has no
                real resolution time to supply; never invented.

        Returns:
            Intent: `kind="single"`, one `Leg` on `venue`,
                `hold_to_resolution=False`, `atomicity="best_effort"`,
                `confidence` and `metadata` carried over unchanged.

        Raises:
            ValueError: If `self.type` is `SignalType.HOLD` — a HOLD
                signal is not an order and has no `Leg` representation.
        """
        if self.type == SignalType.HOLD:
            raise ValueError("cannot convert a HOLD signal to an Intent")

        leg = Leg(
            market_id=self.market_id,
            outcome=self.outcome,
            side="BUY" if self.type == SignalType.BUY else "SELL",
            limit_price=self.price,
            size_usd=self.size,
            venue=venue,
        )
        return Intent(
            kind="single",
            legs=[leg],
            hold_to_resolution=False,
            atomicity="best_effort",
            confidence=self.confidence,
            expected_resolution_ts=expected_resolution_ts,
            metadata=dict(self.metadata),
        )


@dataclass
class Leg:
    """One order-shaped component of a multi-leg `Intent` (PLAN.md D7).

    A `Leg` is a strategy-layer intent, not yet a validated venue order —
    `OrderRouter` (T14) is responsible for turning an approved `Intent`'s
    legs into `app.venues.types.OrderRequest`s. Units follow GUARDRAILS.md
    §4: `limit_price` is a probability in [0.0, 1.0]; sizes are contracts
    (each pays $1.00 at resolution) or USD, never both.

    Attributes:
        market_id: Venue-native market identifier.
        outcome: Outcome being traded, e.g. `"YES"`/`"NO"`. This is the
            DISPLAY label: `normalize_outcome()` runs on it in
            `__post_init__` (Phase-1 remediation FIX 1, extended by
            T21d), so any case-insensitive spelling of `"yes"`/`"no"`
            becomes `"YES"`/`"NO"` and surrounding whitespace is
            stripped from every label — but a bundle's named outcome
            keeps the venue's own casing. Everything that KEYS on this
            leg's outcome (`Position.position_id`, `Intent`'s bundle
            distinct-outcome check, the engine's `_current_prices` and
            SELL-leg position lookup) runs it through
            `outcome_key()` instead, so `"TRUMP"` and `"Trump"` are one
            outcome no matter which venue spelled which.
        side: `"BUY"` or `"SELL"`.
        limit_price: Limit price, a probability in [0.0, 1.0].
        size_contracts: Size in contracts, or `None` if not yet sized.
        size_usd: Size in USD, or `None` if not yet sized. Exactly one of
            `size_contracts`/`size_usd` may be set at construction; both
            may be `None`, meaning this leg is sized later by
            `BaseStrategy.calculate_position_size`.
        venue: `"polymarket"` or `"kalshi"`. Defaults to `"polymarket"`
            since same-venue legs (complement, bundle) are the common
            case; a `cross_venue` intent's second leg must set this
            explicitly.
    """

    market_id: str
    outcome: str
    side: OrderSide
    limit_price: float
    size_contracts: float | None = None
    size_usd: float | None = None
    venue: VenueId = "polymarket"

    def __post_init__(self) -> None:
        """Normalize `outcome` casing and validate the size fields.

        Raises:
            ValueError: If both `size_contracts` and `size_usd` are set.
        """
        self.outcome = normalize_outcome(self.outcome)
        if self.size_contracts is not None and self.size_usd is not None:
            raise ValueError(
                "at most one of size_contracts/size_usd may be set at "
                f"construction, got size_contracts={self.size_contracts!r} "
                f"and size_usd={self.size_usd!r}"
            )


@dataclass
class Intent:
    """Multi-leg execution unit that replaces the one-leg `Signal` (PLAN.md D7).

    `on_market_data` may return a `Signal` (back-compat, normalized via
    `Signal.to_intent()`) or an `Intent` directly. `binary_complement_arbitrage`
    emits a `"complement"` intent (YES+NO on one venue);
    `multi_outcome_bundle_arbitrage` emits a `"bundle"` intent (N legs, one
    market); cross-venue arbitrage emits a `"cross_venue"` intent (YES on
    venue A, NO on venue B). Position ids downstream become
    `f"{venue}:{market_id}:{outcome}"`.

    Attributes:
        kind: Intent shape — see `IntentKind`.
        legs: The intent's `Leg`s. Validated against `kind` in
            `__post_init__` (see there for the exact per-kind rules).
        hold_to_resolution: If `True`, legs are held to market resolution
            rather than closed early (PLAN.md D8: the riskless complement
            form is buy-and-hold-to-resolution, not buy/sell).
        atomicity: `"all_or_none"` or `"best_effort"` — see `AtomicityMode`.
        confidence: Strategy confidence level, in [0.0, 1.0].
        expected_resolution_ts: Aware UTC expected resolution time, or
            `None` if unknown.
        metadata: Additional intent metadata. Free-form, EXCEPT for the
            scoring-contract keys defined at the top of this module
            (`SCORING_EDGE_KEY` and friends): a strategy that wants its
            intent ranked publishes a PRE-risk edge under those and
            declares any identity risk separately, so
            `app.services.scoring` can apply every haircut in one place.

    Raises (in `__post_init__`):
        ValueError: If `legs` is empty, if `kind`'s per-kind leg
            constraints are violated, or if `confidence` is out of
            `[0.0, 1.0]`.
        TypeError: If `expected_resolution_ts` is set but not a
            `datetime`.
        ValueError: If `expected_resolution_ts` is set but naive.
    """

    kind: IntentKind
    legs: list[Leg]
    hold_to_resolution: bool
    atomicity: AtomicityMode
    confidence: float
    expected_resolution_ts: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate leg count/shape against `kind`, `confidence`, and
        `expected_resolution_ts` (PLAN.md D7 / R9).
        """
        if len(self.legs) < 1:
            raise ValueError("Intent must have at least one leg")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be between 0 and 1, got {self.confidence}")
        if self.expected_resolution_ts is not None:
            ensure_aware(self.expected_resolution_ts)

        if self.kind == "complement":
            if len(self.legs) != 2:
                raise ValueError(
                    f"complement intent must have exactly 2 legs, got {len(self.legs)}"
                )
            leg_a, leg_b = self.legs
            if leg_a.venue != leg_b.venue:
                raise ValueError("complement intent legs must be on the same venue")
            if leg_a.market_id != leg_b.market_id:
                raise ValueError("complement intent legs must be on the same market")
            outcomes = {leg_a.outcome, leg_b.outcome}
            if outcomes != {"YES", "NO"}:
                raise ValueError(
                    f"complement intent legs must be outcomes {{'YES', 'NO'}}, "
                    f"got {outcomes}"
                )
        elif self.kind == "cross_venue":
            if len(self.legs) != 2:
                raise ValueError(
                    f"cross_venue intent must have exactly 2 legs, got {len(self.legs)}"
                )
            leg_a, leg_b = self.legs
            if leg_a.venue == leg_b.venue:
                raise ValueError("cross_venue intent legs must be on different venues")
            # Phase-1 remediation FIX 1: `complement` already required
            # exactly {"YES", "NO"}; `cross_venue` had NO outcome check
            # at all, so two YES legs (or any non-YES/NO pairing) on two
            # venues would construct without complaint. `Leg.outcome` is
            # normalized in its own `__post_init__`, which has already
            # run by the time this reads it, so this check is meaningful
            # regardless of which venue's casing convention produced it.
            outcomes = {leg_a.outcome, leg_b.outcome}
            if outcomes != {"YES", "NO"}:
                raise ValueError(
                    f"cross_venue intent legs must be outcomes {{'YES', 'NO'}}, "
                    f"got {outcomes}"
                )
        elif self.kind == "bundle":
            if len(self.legs) < 3:
                raise ValueError(
                    f"bundle intent must have at least 3 legs, got {len(self.legs)}"
                )
            venues = {leg.venue for leg in self.legs}
            market_ids = {leg.market_id for leg in self.legs}
            if len(venues) != 1 or len(market_ids) != 1:
                raise ValueError("bundle intent legs must all be on the same market")
            # T21d: distinctness is an IDENTITY question, so it is asked
            # of `outcome_key()` and not of the display label. Two legs
            # spelled `"Trump"` and `"trump"` are the SAME outcome
            # bought twice, not a two-outcome bundle — and left
            # undetected they would collapse onto one `position_id`
            # downstream while the bundle's own arithmetic still assumed
            # two independent legs.
            outcome_keys = [outcome_key(leg.outcome) for leg in self.legs]
            if len(set(outcome_keys)) != len(outcome_keys):
                raise ValueError("bundle intent legs must have distinct outcomes")
        elif self.kind != "single":
            raise ValueError(f"unknown intent kind: {self.kind!r}")


@dataclass
class MarketSnapshot:
    """Point-in-time snapshot of market data for strategy evaluation.

    Attributes:
        market_id: Polymarket condition ID.
        token_id: Token ID for the specific outcome.
        timestamp: Snapshot timestamp.
        yes_price: Current YES token price.
        no_price: Current NO token price.
        yes_bid: Best bid for YES token.
        yes_ask: Best ask for YES token.
        no_bid: Best bid for NO token.
        no_ask: Best ask for NO token.
        spread: Bid-ask spread.
        volume: Recent trading volume.
        volume_24h: 24-hour trading volume.
        open_interest: Total open interest.
        orderbook: Full orderbook data.
        recent_trades: List of recent trades.
        question: Market question text.
        category: Market category.
        end_date: Market end date.
        resolution_rules: Market resolution criteria.
        venue: `"polymarket"` or `"kalshi"`. Defaults to `"polymarket"`
            (every snapshot predates T06 was single-venue).
        book: Normalized `OrderBook` for this (market, outcome), if
            available; `None` until depth is wired in (PLAN.md D10).
    """

    market_id: str
    token_id: str
    timestamp: datetime
    yes_price: float
    no_price: float
    yes_bid: float | None = None
    yes_ask: float | None = None
    no_bid: float | None = None
    no_ask: float | None = None
    spread: float | None = None
    volume: float = 0.0
    volume_24h: float = 0.0
    open_interest: float | None = None
    orderbook: dict[str, Any] = field(default_factory=dict)
    recent_trades: list[dict[str, Any]] = field(default_factory=list)
    question: str = ""
    category: str | None = None
    end_date: datetime | None = None
    resolution_rules: str | None = None
    venue: VenueId = "polymarket"
    book: OrderBook | None = None

    def __post_init__(self) -> None:
        """Validate that `timestamp` is aware UTC.

        This is the naive-datetime tripwire (PLAN.md R9): a naive
        `timestamp` anywhere in the backtesting or execution path is a
        defect, not an environment quirk (GUARDRAILS.md §4).

        Raises:
            TypeError: If `timestamp` is not a `datetime`.
            ValueError: If `timestamp` is naive (`tzinfo is None`).
        """
        ensure_aware(self.timestamp)

    @property
    def mid_price(self) -> float:
        """Calculate mid price from bid/ask or use yes_price."""
        if self.yes_bid is not None and self.yes_ask is not None:
            return (self.yes_bid + self.yes_ask) / 2
        return self.yes_price

    @property
    def implied_probability(self) -> float:
        """Return YES price as implied probability."""
        return self.yes_price


class BaseStrategy(ABC):
    """Abstract base class for all trading strategies.

    Strategies implement market analysis logic and generate trading signals.
    They can be used for both backtesting and live trading.

    Example:
        ```python
        class MomentumStrategy(BaseStrategy):
            def on_market_data(self, snapshot: MarketSnapshot) -> Signal | None:
                if snapshot.yes_price > self.config.get("threshold", 0.7):
                    return Signal(
                        type=SignalType.BUY,
                        market_id=snapshot.market_id,
                        token_id=snapshot.token_id,
                        outcome="Yes",
                        price=snapshot.yes_price,
                        size=100.0,
                        confidence=0.8,
                    )
                return None
        ```
    """

    name: str = "base"
    description: str = "Base strategy interface"
    version: str = "1.0.0"

    #: The `EDGE_BASIS_KEY` value this strategy stamps on EVERY intent it
    #: emits, or `None` when it does not always declare one.
    #:
    #: This exists so a caller can ask "can `scan()` score this strategy
    #: at all?" WITHOUT running it. `_published_edge` reads the basis off
    #: each intent's metadata at scoring time, which is the right seam
    #: for scoring but useless to a route deciding whether to accept a
    #: `?strategies=` name — by then the pass has already run and skipped
    #: everything.
    #:
    #: A strategy whose basis is unconditional MUST declare it here and
    #: stamp its metadata FROM this attribute, so the two cannot drift;
    #: `tests/strategies/test_declared_edge_basis.py` pins that. Leave it
    #: `None` when the basis genuinely varies per intent — absence means
    #: "ask the intent", which is the pre-existing behaviour.
    declared_edge_basis: str | None = None

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize the strategy with configuration.

        Args:
            config: Strategy-specific configuration parameters.
        """
        self.config = config or {}
        self._is_initialized = False
        self._trade_count = 0
        self._total_pnl = 0.0

    @property
    def is_initialized(self) -> bool:
        """Check if strategy has been initialized."""
        return self._is_initialized

    @abstractmethod
    def on_market_data(self, snapshot: MarketSnapshot) -> Signal | Intent | None:
        """Process market data and optionally generate a trading signal.

        This is the main strategy logic method. Called for each market
        data update during backtesting or live trading.

        A strategy may return a one-leg `Signal` (back-compat; the engine
        normalizes it via `Signal.to_intent()`) or a multi-leg `Intent`
        directly (PLAN.md D7) — e.g. `binary_complement_arbitrage` returns
        a `"complement"` `Intent` with YES+NO legs.

        Args:
            snapshot: Current market state snapshot.

        Returns:
            Signal | Intent | None: A `Signal` or `Intent` if a trade
                should be executed, `None` otherwise.
        """
        pass

    @abstractmethod
    def calculate_position_size(
        self,
        signal: Signal,
        portfolio_value: float,
        positions: dict[str, Any],
    ) -> float:
        """Calculate the appropriate position size for a signal.

        Implements position sizing logic based on portfolio risk management.

        Args:
            signal: The trading signal to size.
            portfolio_value: Current total portfolio value.
            positions: Dictionary of current open positions.

        Returns:
            Position size in dollars.
        """
        pass

    def on_trade_executed(self, trade: dict[str, Any]) -> None:  # noqa: ARG002
        """Called when a trade is successfully executed.

        Override to implement post-trade logic like logging or state
        updates. The base implementation only bumps `_trade_count`; `trade`
        is unused here on purpose — it exists for overriders, not for this
        base hook.

        Args:
            trade: Executed trade details including:
                - market_id: Market identifier
                - token_id: Token traded
                - side: BUY or SELL
                - price: Execution price
                - size: Trade size
                - fee: Transaction fee
                - timestamp: Execution time
        """
        self._trade_count += 1

    def on_position_closed(self, position: dict[str, Any], pnl: float) -> None:  # noqa: ARG002
        """Called when a position is closed.

        Override to implement position close logic like performance
        tracking. The base implementation only accumulates `_total_pnl`;
        `position` is unused here on purpose — it exists for overriders,
        not for this base hook.

        Args:
            position: Closed position details including:
                - market_id: Market identifier
                - token_id: Token held
                - entry_price: Average entry price
                - exit_price: Exit price
                - size: Position size
                - entry_time: When position was opened
                - exit_time: When position was closed
            pnl: Realized profit/loss in dollars.
        """
        self._total_pnl += pnl

    def reset(self) -> None:
        """Reset strategy state for a new backtest or trading session.

        Called before starting a new backtest run or when restarting
        live trading. Override to reset strategy-specific state.
        """
        self._is_initialized = False
        self._trade_count = 0
        self._total_pnl = 0.0

    async def initialize(self) -> None:
        """Async initialization for loading data or models.

        Override for strategies that need async setup like loading
        ML models or historical data.
        """
        self._is_initialized = True

    async def cleanup(self) -> None:
        """Async cleanup for releasing resources.

        Override for strategies that hold resources like database
        connections or file handles.
        """
        self._is_initialized = False

    def validate_config(self) -> bool:
        """Validate strategy configuration.

        Override to implement config validation logic.

        Returns:
            True if configuration is valid, False otherwise.
        """
        return True

    def get_default_config(self) -> dict[str, Any]:
        """Get default configuration values.

        Override to provide strategy-specific defaults.

        Returns:
            Dictionary of default configuration values.
        """
        return {}

    def get_stats(self) -> dict[str, Any]:
        """Get current strategy statistics.

        Returns:
            Dictionary containing strategy performance stats.
        """
        return {
            "name": self.name,
            "version": self.version,
            "trade_count": self._trade_count,
            "total_pnl": self._total_pnl,
            "is_initialized": self._is_initialized,
        }

    def __repr__(self) -> str:
        """String representation of the strategy."""
        return f"{self.__class__.__name__}(name={self.name}, config={self.config})"
