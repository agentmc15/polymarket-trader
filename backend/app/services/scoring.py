"""Opportunity scoring (PLAN.md D10, T19; rebased on one edge basis in T31).

`score(intent, ctx)` is the ONE place every strategy's output is reduced
to a single, comparable `composite` ranking number, regardless of which
strategy produced it or which venue(s) it touches.

THE INVARIANT THIS MODULE OWES ITS CALLERS
------------------------------------------
    Two intents with the same `composite` represent the same expected
    risk-adjusted return PER DOLLAR OF CAPITAL LOCKED UP, whichever
    strategy produced them.

`GET /api/v1/arbitrage/opportunities` sorts one list by `composite` and a
human reads that order as a ranking. So the number has to mean one thing.
Three separate things had to be fixed for it to (see "WHAT REMAINS
INCOMPARABLE" for the honest limits of the claim):

1. ONE EDGE BASIS. Strategies publish a PRE-RISK edge and this module
   applies every haircut — `app.strategies.base`'s scoring contract
   (`SCORING_EDGE_KEY`, `IDENTITY_CONFIDENCE_KEY`,
   `IDENTITY_WORST_CASE_LOSS_KEY`). Before T31, `cross_venue_arbitrage`
   published a number already multiplied by its link confidence while
   the three single-market strategies published one that was not, and
   `composite` ranked all four against each other anyway —
   `multi_outcome_bundle_arbitrage`'s own comment said "do not compare
   this value to `cross_venue_arbitrage`'s `net_edge`", and `composite`
   was the sole consumer doing exactly that.

   THE ALTERNATIVE WAS CONSIDERED AND REJECTED: leaving strategies to
   publish POST-risk edges and teaching the scorer which risks each one
   had already priced in. That is a smaller diff, but it makes the
   scorer carry a per-strategy table of "net of what?", which is
   precisely the coupling that produced the bug — and it fails silently
   the day a fifth strategy is added, because a new strategy publishing
   an unhaircut edge would be scored as if it were haircut (or the
   reverse) with nothing in the code to notice. Under the pre-risk rule
   a new strategy that says nothing about identity risk is scored as
   having none, which is both the common case and the safe reading:
   nothing is silently double-discounted, and a strategy that DOES carry
   the risk has to say so in the payload to have it priced.

2. IDENTITY RISK IS PRICED EXACTLY ONCE, HERE. `_risk_adjusted_edge`
   applies `edge * p - (1 - p) * worst_case_loss` — the same formula
   `cross_venue_arbitrage` used to apply internally — to any intent that
   declares `p_same_resolution`, and to no other. `resolution_risk`
   correspondingly no longer carries the old `+0.25 if kind ==
   "cross_venue" and confidence < 0.95` term: that term was the SECOND
   discount, applied to an edge that had already been haircut, so a
   cross-venue opportunity paid for its identity risk twice while every
   single-market strategy paid for it neither time. It was not removed
   in favour of nothing — it was removed because the risk it stood for
   is now priced upstream of it, in dollars, at the intent's own
   confidence rather than at a step at 0.95. Its economic content
   survives: that term existed because below ~0.95 confidence a
   cross-venue pair is usually negative-EV outright rather than merely
   riskier, and `edge * p - (1 - p) * worst_case_loss` reproduces that
   directly — a pair that cannot cover its own haircut now scores a
   NEGATIVE `net_edge`, a negative `composite`, and sorts below every
   viable trade instead of being nudged down a rank.
   `CrossVenueArbitrageStrategy`'s `min_net_edge` gate still refuses to
   emit such a pair at all; scoring no longer needs a proxy for a gate
   that already exists.

3. ONE DENOMINATOR. `annualized_return` is a RETURN — expected profit
   divided by the capital actually committed — not the per-contract
   dollar edge PLAN.md D10 wrote as `net_edge / hours * 8760`. That
   formula never divided by capital at all, which `app.services.scanner`
   already documented as "an adequate approximation for
   binary_complement_arbitrage/cross_venue_arbitrage ... but NOT here"
   for the near-resolution pass, and worked around by substituting
   `settlement_edge`'s own capital-normalized figure. Both passes'
   rows land in ONE list sorted by `composite`, so before T31 that sort
   compared USD-per-contract-per-year against a dimensionless
   return-per-year. Normalizing here makes the general pass produce the
   same unit the near-resolution pass was already producing, and makes
   the invariant above literally true rather than true-when-every-leg-
   happens-to-cost-about-a-dollar.

THE FORMULA::

    units             = max over legs of contracts requested
                        (all four arbitrage strategies size equal
                         contracts on every leg, so this is the number
                         of complete "units" bought)
    edge              = strategy-published PRE-risk edge, USD per unit
    net_edge          = edge * p - (1 - p) * worst_case_loss
                        (p = 1.0, i.e. net_edge == edge, for any intent
                         that declares no identity risk)
    annualized_return = (net_edge * units / capital_lockup_usd)
                        / max(hours_to_resolution, min_hours) * 8760
    fill_confidence   = min over legs of (contracts fillable within
                        max_slippage_bps of the leg's limit) / requested
    resolution_risk   = 1 - (1 - 0.15) ** (number of DISTINCT markets)
                        + 0.25 if any leg's market has rules_text < 200 chars
                        + 0.20 if any leg's market has resolution_source is None
                        + 0.15 if hours_to_resolution < 6  (dispute window)
                        , capped at 1.0
    composite         = annualized_return * fill_confidence * (1 - resolution_risk)

WHY THE BASE RISK COMPOUNDS OVER DISTINCT MARKETS. `_RESOLUTION_RISK_BASE`
is the chance a venue mis-adjudicates, delays, or disputes a resolution.
A complement or a bundle is exposed to that once — every leg redeems off
ONE question on ONE venue. A cross-venue pair is exposed to it twice, on
two venues that adjudicate independently, and `1 - (1 - 0.15)**2 =
0.2775` is that exposure, not a penalty chosen to keep cross-venue in its
place. It is also the only term that still distinguishes a cross-venue
intent from a same-venue one, and the distinction it draws is small and
derived: at equal capital and equal time, a cross-venue pair must earn
`1 / 0.85 = 1.176x` the edge of a same-venue pair to rank equally —
exactly the price of the second venue's settlement risk, and nothing
more. Treating the two venues' adjudication errors as independent
OVERSTATES the risk (both venues read the same news), so this is the
conservative end of the range.

WHAT REMAINS INCOMPARABLE, AND WHY IT IS LABELLED RATHER THAN HIDDEN.
`composite` is an honest ranking of expected risk-adjusted return per
dollar, but two residues do not reduce to one number:

  a. `p_same_resolution` IS AN ESTIMATE, and the fee/gas terms are not.
     A cross-venue intent's `net_edge` is `edge * p - (1 - p) * L` where
     `p` is `app.services.matching`'s link confidence — a deterministic
     similarity score in `[0, 1]`, not a measured or calibrated
     probability. Every other term in every strategy's edge is an
     observed price or a published rate. So a cross-venue row and a
     complement row with the same `composite` have the same expected
     return only to the extent that score is calibrated, and the
     cross-venue one additionally carries model risk the complement does
     not. `OpportunityScore.edge_basis` says which of the two a row is
     (`"observed_costs"` / `"identity_estimated"`), and the originating
     strategy stamps the same label in `Intent.metadata[EDGE_BASIS_KEY]`
     so it reaches `/opportunities`' payload, not just the persisted
     score. Sorting by one column is fine; trusting a cross-venue row
     over a same-venue row on a 3% `composite` difference is not, and
     the label is there so an operator can see that without reading this
     module.
  b. THE TWO SCANNER PASSES FLOOR TIME DIFFERENTLY. This module floors
     at `settings.min_hours_for_annualization` (6h);
     `app.services.scanner.near_resolution_pass` substitutes
     `settlement_edge`'s own figure, floored at
     `settings.settlement_delay_hours` (24h), because a near-resolution
     trade's capital is locked until the venue actually settles, not
     until `close_time`. Both are returns per dollar per year — the unit
     matches — but a near-resolution row's annualization is floored
     harder, i.e. deliberately conservative relative to a general-pass
     row of the same true duration.

DEPTH_SOURCE AND LINK_STATUS RIDE THROUGH TO THE PAYLOAD (GUARDRAILS.md
§1.7, PLAN.md D9). `OpportunityScore.depth_source` is `"recorded"` only
when EVERY leg's book was `"recorded"`; `"synthetic"` when none were
observed at all (the conservative default — labeling a result
`"recorded"` on the strength of zero recorded books is exactly the
hidden assumption §1.7 forbids); `"mixed"` otherwise. A `fill_confidence`
computed against invented (`synthesize_book`) depth is itself invented,
so this label must reach `/arbitrage/opportunities`, not just a log line.
`OpportunityScore.link_status` carries whatever `intent.metadata
["link_status"]` says (stamped by `app.services.scanner` for a
cross-venue intent it can attribute to an `EventLink`) — PLAN.md D9
permits a `"proposed"` link at confidence >= 0.85 to be SCORED in paper
mode even though nothing may EXECUTE on it, so an operator reading
`/opportunities` must be able to see that an item is unexecutable before
they try to route it, not discover it from a rejection.

THE ANNUALIZATION FLOOR (`settings.min_hours_for_annualization`, default
6h). Without it, a market resolving in ten minutes reports a
six-digit-percent annualized return and dominates every ranking, even
though ten minutes of edge is not six more hours of the same edge
repeating for a year. Below the floor, `annualized_return` is computed
AS IF `hours_to_resolution` were the floor value — i.e. it is capped, not
extrapolated past the floor — so a 6-minute and a 6-hour opportunity with
the same `net_edge` report the identical `annualized_return`; the raw
`hours_to_resolution` field is still reported UNFLOORED, so the near-term
bucket is still visible, only the annualization stops accelerating.

A MARKET ALREADY RESOLVED, OR PAST `close_time`, IS NEVER SCORED (T09:
no fill can occur on a resolved market, so surfacing one as an
opportunity would be a phantom). `score()` raises `UnscorableIntent` for
such an intent; `app.services.scanner.scan()` catches it and skips the
intent rather than persisting a phantom row.

THE ONE DELIBERATE EXCEPTION (T20, PLAN.md D10(c)): `score(...,
allow_past_close=True)` skips ONLY the `close_time` half of that guard,
never the `"resolved"` half. `app.services.scanner.near_resolution_pass`
is the one caller that passes it, and only for `settlement_edge`'s
"outcome already determined" intents — a capital-lockup trade whose
entire premise is that `close_time` has already passed (the scheduled
event date is behind us) while the venue has not yet finalized the
result (Polymarket's own payload commonly still reports `status="open"`
through the UMA optimistic-oracle challenge window; see `score()`'s
docstring for the full reasoning). A market that is genuinely
`"resolved"` is still refused unconditionally — this flag never touches
T09's actual rule, only the close_time proxy for it.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from app.config import Settings
from app.strategies.base import (
    EDGE_BASIS_IDENTITY_ESTIMATED,
    EDGE_BASIS_KEY,
    EDGE_BASIS_OBSERVED,
    IDENTITY_CONFIDENCE_KEY,
    IDENTITY_WORST_CASE_LOSS_KEY,
    SCORING_EDGE_KEY,
    SCORING_EDGE_LEGACY_KEY,
    Intent,
    Leg,
)
from app.venues.types import OrderBook, VenueMarket, WalkSide

#: Hours in a 365-day year — the same annualization constant
#: `app.strategies.cross_venue_arbitrage.HOURS_PER_YEAR` uses. Not a fee,
#: not a rate: a unit conversion, kept in sync by hand rather than
#: imported (scoring must not depend on any one strategy module).
HOURS_PER_YEAR = 8760.0

#: `resolution_risk`'s unconditional floor (PLAN.md D10), PER DISTINCT
#: MARKET — the chance ONE venue mis-adjudicates, delays, or disputes ONE
#: question, even with perfect documentation and a certain link. An
#: intent spanning N distinct markets is exposed N times over, so the
#: base term is `1 - (1 - 0.15)**N` (see the module docstring's "WHY THE
#: BASE RISK COMPOUNDS OVER DISTINCT MARKETS"). N is 1 for a complement,
#: a bundle and a single-leg intent — every leg redeems off one question
#: — and 2 for a cross-venue pair.
_RESOLUTION_RISK_BASE = 0.15

#: Below this many characters, a market's `rules_text` is too thin to
#: adjudicate an edge case with confidence (PLAN.md D10).
_RULES_TEXT_MIN_CHARS = 200
_RULES_TEXT_PENALTY = 0.25

#: No named resolution authority at all.
_NO_RESOLUTION_SOURCE_PENALTY = 0.20

#: T31 removed `_CROSS_VENUE_CONFIDENCE_FLOOR = 0.95` /
#: `_CROSS_VENUE_PENALTY = 0.25` from `_resolution_risk`. They were the
#: SECOND discount on a `net_edge` that `cross_venue_arbitrage` had
#: already haircut by the same link confidence; identity risk is now
#: priced once, in dollars, by `_risk_adjusted_edge`. See the module
#: docstring's point 2 — this is not a relaxation, it is a relocation.

#: PLAN.md D10's dispute-window proxy: inside 6 hours of resolution,
#: Polymarket's proposed-but-challengeable window and Kalshi's
#: settlement timer are both live risk, not yet-settled certainty.
_DISPUTE_WINDOW_HOURS = 6.0
_DISPUTE_WINDOW_PENALTY = 0.15

#: `resolution_risk` never exceeds this, however many penalties stack.
_RESOLUTION_RISK_CAP = 1.0

#: Absolute epsilon (probability) for the crossed-book and
#: within-slippage-bound comparisons below. Mirrors
#: `app.execution.fill_engine`'s own `_PRICE_EPSILON` — duplicated
#: rather than imported so this module does not depend on the fill
#: engine's internals for a two-line comparison.
_PRICE_EPSILON = 1e-12

#: `OrderBook.metadata` key an upstream producer (a recorded snapshot
#: assembled from two reads, or one whose crossing side was filtered on
#: the way in) sets to flag a cross this book's own bid/ask comparison
#: cannot see. Mirrors `app.execution.fill_engine.CROSSED_QUOTES_KEY`'s
#: value exactly (duplicated for the same reason as the epsilon above).
_CROSSED_QUOTES_KEY = "crossed_quotes"


class UnscorableIntent(ValueError):
    """`score()` cannot produce a meaningful `OpportunityScore` for this intent.

    Raised — never silently defaulted — when scoring would otherwise have
    to invent a number: no `expected_resolution_ts`, no `VenueMarket` on
    record for one of the intent's legs, or a leg's market that is
    already resolved or past its `close_time` (T09: no fill can occur on
    a resolved market, so it must never be surfaced as an opportunity).
    Callers (`app.services.scanner.scan()`) catch this and skip the
    intent rather than persisting a phantom row.
    """


@dataclass(frozen=True)
class ScoreContext:
    """Read-only market state `score()` evaluates one `Intent` against.

    Attributes:
        now: Aware UTC "as of" time. `hours_to_resolution` and the
            resolved/`close_time` check are both computed against this,
            not against `app.utils.time.utcnow()`, so a test (or a
            scanner run) can score deterministically against a fixed
            instant.
        books: `(venue, market_id, outcome)` -> the current `OrderBook`
            for that leg, if known. A leg with no entry here scores
            `fill_confidence` contribution `0.0` for that leg (PLAN.md
            R4: a price the book cannot supply is not a price) rather
            than raising — unlike a missing `VenueMarket`, a missing book
            does not mean the market is unscorable, only unfillable.
        markets: `(venue, market_id)` -> the current `VenueMarket` for
            every market any leg references. Every leg's market MUST be
            present here — `score()` raises `UnscorableIntent` otherwise
            — since `resolution_risk` and the resolved/`close_time` guard
            both need it.
        settings: The live `Settings`, for `min_hours_for_annualization`
            and `max_slippage_bps`.
    """

    now: datetime
    books: Mapping[tuple[str, str, str], OrderBook]
    markets: Mapping[tuple[str, str], VenueMarket]
    settings: Settings


@dataclass(frozen=True)
class OpportunityScore:
    """The scored, rankable form of one `Intent` (PLAN.md D10).

    Every field here is what `app.models.intent.IntentRecord.score`
    persists and what `GET /api/v1/arbitrage/opportunities` returns
    per-row (`app.api.routes.arbitrage`) — this dataclass IS the payload
    contract, not an internal detail reshaped later.

    Attributes:
        net_edge: Per-UNIT edge, USD, net of fees, gas AND — for an
            intent that declared identity risk — the resolution-mismatch
            haircut this module applied (`edge * p - (1 - p) *
            worst_case_loss`). The strategy publishes the PRE-risk half
            (`Intent.metadata[SCORING_EDGE_KEY]`) and this module applies
            the haircut, so "net of what?" has one answer for every
            strategy. `0.0` if the strategy published no edge at all (a
            non-arbitrage intent has no riskless edge for this field to
            report). A "unit" is one contract of every leg.
        annualized_return: `net_edge` expressed as a fraction of the
            capital actually committed, per year: `net_edge * units /
            capital_lockup_usd / max(hours, min_hours) * 8760`, floored
            per `settings.min_hours_for_annualization` — see the module
            docstring's "THE ANNUALIZATION FLOOR" and its point 3 on why
            this divides by capital where PLAN.md D10's literal formula
            did not. `0.0` when the intent commits no capital at all
            (nothing to earn a return ON).
        hours_to_resolution: UNFLOORED hours from `ScoreContext.now` to
            `Intent.expected_resolution_ts`. Always positive — an
            already-past resolution time raises `UnscorableIntent`
            before this is computed.
        fill_confidence: In `[0.0, 1.0]`. The MINIMUM, across legs, of
            (contracts fillable within `settings.max_slippage_bps` of
            that leg's limit) / (contracts requested). A leg this
            context has no book for, or whose book is crossed, scores
            `0.0` for that leg.
        resolution_risk: In `[0.0, 1.0]`. See the module docstring.
        capital_lockup_usd: Total notional committed across every leg —
            `sum(leg.limit_price * leg.size_contracts)` for a
            contract-sized leg, `leg.size_usd` for a dollar-sized one.
        composite: `annualized_return * fill_confidence * (1 -
            resolution_risk)` — the single number `/opportunities` sorts
            by. Can be negative (a negative `net_edge` intent should
            never have been emitted by a strategy, but scoring does not
            hide one that was).
        link_status: `intent.metadata.get("link_status")`, verbatim
            (stringified), or `None` if absent. See the module docstring
            — PLAN.md D9 allows scoring, but never executing, a
            `"proposed"` link's intent, and this is how a caller
            distinguishes the two without inferring it from confidence.
        depth_source: `"recorded"`, `"synthetic"`, or `"mixed"` — the
            aggregate `OrderBook.depth_source` across every leg this
            context could find a book for; `"synthetic"` (the
            conservative default) if none could be found at all.
        edge_basis: `"observed_costs"` when every term of `net_edge` is
            an observed price or a published fee/gas rate, or
            `"identity_estimated"` when it also contains a haircut driven
            by an ESTIMATED probability (`p_same_resolution`). This is
            the residue `composite` cannot express — see the module
            docstring's "WHAT REMAINS INCOMPARABLE" (a). Derived from
            whether the intent DECLARED an identity confidence, not
            copied from the strategy's own `EDGE_BASIS_KEY` claim, so an
            intent cannot label its way out of the caveat (and one that
            claims the caveat while declaring no confidence for scoring
            to price is refused outright — `_risk_adjusted_edge`).
    """

    net_edge: float
    annualized_return: float
    hours_to_resolution: float
    fill_confidence: float
    resolution_risk: float
    capital_lockup_usd: float
    composite: float
    link_status: str | None
    depth_source: str
    edge_basis: str


def score(
    intent: Intent, ctx: ScoreContext, *, allow_past_close: bool = False
) -> OpportunityScore:
    """Score one `Intent` against current market/book state (PLAN.md D10).

    Args:
        intent: The strategy-produced intent to score. Its legs must
            already be sized (`size_contracts`/`size_usd` set) — an
            unsized leg contributes nothing to `fill_confidence` or
            `capital_lockup_usd` (see `_leg_contracts`).
        ctx: The market/book state to score against.
        allow_past_close: `False` (default) preserves T19's original
            rule exactly: a leg's market past its `close_time` is
            unscorable, same as one already `"resolved"`. Pass `True`
            ONLY from `app.services.scanner.near_resolution_pass`
            (T20/PLAN.md D10(c)) — a capital-lockup ("outcome already
            determined") intent is BY CONSTRUCTION on a market whose
            `close_time` has passed (that is what "outcome already
            determined" means: the scheduled event date is behind us,
            trading has stopped, and the venue has not yet finalized the
            result), and Polymarket's own payload frequently still
            reports `status="open"` there for as long as the UMA
            optimistic-oracle challenge window is live (`app/venues/
            polymarket/adapter.py`'s `close_time` is Gamma's `endDate`,
            distinct from its `closed`/`resolved` flags) — so refusing
            to score every such intent would silently drop the ONE
            strategy this scanner pass exists for. A market that is
            actually `status == "resolved"` is STILL always refused
            regardless of this flag: T09's real rule ("no fill can occur
            on a resolved market") is about resolution, not the
            close_time proxy, and this flag only ever loosens the proxy,
            never the resolved check itself.

    Returns:
        OpportunityScore: The scored result.

    Raises:
        UnscorableIntent: If `intent.expected_resolution_ts` is `None`,
            if any leg's `(venue, market_id)` is not in `ctx.markets`, if
            any leg's market is already `"resolved"`, if (unless
            `allow_past_close`) any leg's market is past its
            `close_time`, or if any scoring-contract metadata value the
            intent DID publish is unreadable as a number or out of range
            (see `_published_edge`/`_risk_adjusted_edge`).
    """
    leg_markets = _leg_markets(intent, ctx, allow_past_close=allow_past_close)
    hours_to_resolution = _hours_to_resolution(intent, ctx.now)
    published_edge = _published_edge(intent)
    net_edge, declares_identity_risk = _risk_adjusted_edge(intent, published_edge)
    capital_lockup_usd = _capital_lockup_usd(intent)
    annualized_return = _annualized_return(
        net_edge, _units(intent), capital_lockup_usd, hours_to_resolution, ctx.settings
    )
    fill_confidence = _fill_confidence(intent, ctx)
    resolution_risk = _resolution_risk(leg_markets, hours_to_resolution)
    composite = annualized_return * fill_confidence * (1.0 - resolution_risk)
    depth_source = _depth_source(intent, ctx)

    raw_link_status = intent.metadata.get("link_status")
    link_status = str(raw_link_status) if raw_link_status is not None else None

    return OpportunityScore(
        net_edge=net_edge,
        annualized_return=annualized_return,
        hours_to_resolution=hours_to_resolution,
        fill_confidence=fill_confidence,
        resolution_risk=resolution_risk,
        capital_lockup_usd=capital_lockup_usd,
        composite=composite,
        link_status=link_status,
        depth_source=depth_source,
        edge_basis=(
            EDGE_BASIS_IDENTITY_ESTIMATED
            if declares_identity_risk
            else EDGE_BASIS_OBSERVED
        ),
    )


def _leg_markets(
    intent: Intent, ctx: ScoreContext, *, allow_past_close: bool = False
) -> list[VenueMarket]:
    """Return one `VenueMarket` per leg, in leg order.

    Args:
        intent: The intent being scored.
        ctx: Supplies `markets`.
        allow_past_close: See `score()`'s docstring. `"resolved"` is
            ALWAYS refused regardless of this flag; only the separate
            `close_time <= ctx.now` proxy is skipped when `True`.

    Returns:
        list[VenueMarket]: One entry per leg (duplicates for legs
            sharing a market, e.g. a same-venue complement).

    Raises:
        UnscorableIntent: If a leg's `(venue, market_id)` is not in
            `ctx.markets`, if that market is `"resolved"`, or (unless
            `allow_past_close`) if its `close_time` is at or before
            `ctx.now` (T09: no fill can occur on a resolved market).
    """
    markets: list[VenueMarket] = []
    for leg in intent.legs:
        key = (leg.venue, leg.market_id)
        market = ctx.markets.get(key)
        if market is None:
            raise UnscorableIntent(
                f"no VenueMarket on record for {key!r}; cannot score an intent "
                "against a market this context never read"
            )
        if market.status == "resolved":
            raise UnscorableIntent(
                f"market {key!r} is resolved; T09 established no fill can occur "
                "on a resolved market, so it is never scored as an opportunity"
            )
        if not allow_past_close and market.close_time <= ctx.now:
            raise UnscorableIntent(
                f"market {key!r} is past close_time; T09 established no fill can "
                "occur on a resolved market, and this context did not opt in "
                "(allow_past_close=True) to the T20 near-resolution exception, so "
                "it is never scored as an opportunity"
            )
        markets.append(market)
    return markets


def _hours_to_resolution(intent: Intent, now: datetime) -> float:
    """Return hours from `now` to `intent.expected_resolution_ts`.

    Args:
        intent: The intent being scored.
        now: Aware UTC "as of" time.

    Returns:
        float: Hours, unfloored. Positive whenever every leg's market
            passed `_leg_markets`'s not-resolved/not-past-close check,
            since a strategy's `expected_resolution_ts` is never later
            than the latest leg's own `close_time`.

    Raises:
        UnscorableIntent: If `intent.expected_resolution_ts` is `None`
            (PLAN.md D10: time-to-resolution is a scoring axis on every
            intent; there is nothing to floor or annualize without it).
    """
    if intent.expected_resolution_ts is None:
        raise UnscorableIntent(
            "intent has no expected_resolution_ts; PLAN.md D10 requires "
            "time-to-resolution on every scored intent"
        )
    return (intent.expected_resolution_ts - now).total_seconds() / 3600.0


def _metadata_float(intent: Intent, key: str) -> float:
    """Return `intent.metadata[key]` as a float, or refuse to score.

    A scoring-contract value a strategy DID publish but that cannot be
    read as a number is not a number to default around: silently
    substituting one would put an invented figure into a ranking a human
    reads as money. `app.services.scanner.scan()` already catches
    `UnscorableIntent` and skips the intent, so refusing here costs one
    row and never the pass.

    Args:
        intent: The intent being scored.
        key: The metadata key to read. Must be present.

    Returns:
        float: The value.

    Raises:
        UnscorableIntent: If the value is not convertible to `float`.
    """
    raw = intent.metadata[key]
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise UnscorableIntent(
            f"intent.metadata[{key!r}] is {raw!r}, which is not a number; "
            "scoring will not invent one in its place"
        ) from exc


def _published_edge(intent: Intent) -> float:
    """Return the strategy-published PRE-RISK per-unit edge, or `0.0`.

    The scoring contract (`app.strategies.base`): a strategy publishes
    `SCORING_EDGE_KEY` — USD per unit, net of fees and gas and net of
    NOTHING ELSE. `SCORING_EDGE_LEGACY_KEY` (`"net_edge"`) is read only
    when the preferred key is absent, and means the same thing;
    `multi_outcome_bundle_arbitrage` published under it before the
    contract was written down, and persisted rows still carry it.

    Scoring never RECOMPUTES an edge from fees/books itself: that would
    require guessing which fee schedule a given `Intent.kind` needs,
    which is exactly the strategy-specific logic PLAN.md D8 already
    lives in. An intent publishing neither key (a directional single-leg
    intent) has no measurable riskless edge for this field.

    Args:
        intent: The intent being scored.

    Returns:
        float: The published pre-risk edge, or `0.0`.

    Raises:
        UnscorableIntent: If a published edge is not a number.
    """
    if SCORING_EDGE_KEY in intent.metadata:
        return _metadata_float(intent, SCORING_EDGE_KEY)
    if SCORING_EDGE_LEGACY_KEY in intent.metadata:
        return _metadata_float(intent, SCORING_EDGE_LEGACY_KEY)
    return 0.0


def _risk_adjusted_edge(intent: Intent, published_edge: float) -> tuple[float, bool]:
    """Apply the identity haircut to `published_edge`. Once, and only here.

    `edge * p - (1 - p) * worst_case_loss`, where `p` is the probability
    the intent's legs settle on the SAME fact. A mismatch does not cost
    the edge — it costs the losing leg's entire stake, which is why the
    second term is a stake and not a margin. This is
    `cross_venue_arbitrage`'s own formula, moved here so it is applied
    exactly once for every strategy rather than once inside one strategy
    and again in `resolution_risk` (see the module docstring's point 2).

    An intent that declares no `IDENTITY_CONFIDENCE_KEY` has no identity
    risk to price — every leg redeems off one question — so the edge is
    returned untouched. `IDENTITY_WORST_CASE_LOSS_KEY` defaults to `0.0`
    only for `p == 1.0`, where it cannot matter; declaring real identity
    risk without saying what a mismatch costs is unscorable, not free.

    Args:
        intent: The intent being scored.
        published_edge: `_published_edge`'s result.

    Returns:
        tuple[float, bool]: The risk-adjusted edge, and whether the
            intent DECLARED identity risk at all — which is what
            `OpportunityScore.edge_basis` reports. Declared-at-`p=1.0`
            still counts as declared: the haircut is zero but the `1.0`
            is still an estimate, and a row whose edge rests on one is
            not the same kind of number as one that does not.

    Raises:
        UnscorableIntent: If `p` is unreadable or outside `[0, 1]`, if a
            declared `worst_case_loss` is unreadable or negative, if
            `p < 1.0` with no `worst_case_loss` declared at all, or if
            the intent CLAIMS an identity-estimated edge basis while
            declaring no confidence for the haircut to use.
    """
    if IDENTITY_CONFIDENCE_KEY not in intent.metadata:
        # The one way the contract could rot back into T31's defect: a
        # strategy that has already haircut its own edge, says so via
        # `EDGE_BASIS_KEY`, but publishes no `p` for this function to
        # apply. Scoring it would take the strategy's post-risk number
        # as a pre-risk one and under-discount it silently, which is the
        # same class of bug in the other direction. Refuse instead.
        if intent.metadata.get(EDGE_BASIS_KEY) == EDGE_BASIS_IDENTITY_ESTIMATED:
            raise UnscorableIntent(
                f"intent claims {EDGE_BASIS_KEY}={EDGE_BASIS_IDENTITY_ESTIMATED!r} "
                f"but published no {IDENTITY_CONFIDENCE_KEY!r}; scoring prices "
                "identity risk itself and cannot verify an edge already haircut "
                "somewhere else"
            )
        return published_edge, False
    p_same = _metadata_float(intent, IDENTITY_CONFIDENCE_KEY)
    if not 0.0 <= p_same <= 1.0:
        raise UnscorableIntent(
            f"intent.metadata[{IDENTITY_CONFIDENCE_KEY!r}] is {p_same}, outside "
            "[0, 1]; it is read as a probability and cannot be clamped into one"
        )
    if p_same >= 1.0:
        return published_edge, True
    if IDENTITY_WORST_CASE_LOSS_KEY not in intent.metadata:
        raise UnscorableIntent(
            f"intent declares {IDENTITY_CONFIDENCE_KEY!r}={p_same} but no "
            f"{IDENTITY_WORST_CASE_LOSS_KEY!r}; a mismatch costs the losing leg's "
            "whole stake and scoring will not assume that stake is zero"
        )
    worst_case_loss = _metadata_float(intent, IDENTITY_WORST_CASE_LOSS_KEY)
    if worst_case_loss < 0.0:
        raise UnscorableIntent(
            f"intent.metadata[{IDENTITY_WORST_CASE_LOSS_KEY!r}] is "
            f"{worst_case_loss}; a loss is not negative"
        )
    return published_edge * p_same - (1.0 - p_same) * worst_case_loss, True


def _units(intent: Intent) -> float:
    """Return how many complete UNITS this intent buys.

    A "unit" is one contract of every leg — the thing the published edge
    is quoted per. All four arbitrage strategies size equal contracts on
    every leg (a complement pair, an N-outcome bundle, a cross-venue
    pair), so the unit count is the per-leg contract count; `max` rather
    than `min` reads a legitimately one-sided intent (a single-leg
    `settlement_edge` capital-lockup trade, or a leg left unsized) as the
    size it actually asked for rather than as zero.

    Args:
        intent: The intent being scored.

    Returns:
        float: The unit count, `0.0` for an entirely unsized intent.
    """
    return max((_leg_contracts(leg) for leg in intent.legs), default=0.0)


def _annualized_return(
    net_edge: float,
    units: float,
    capital_lockup_usd: float,
    hours_to_resolution: float,
    settings: Settings,
) -> float:
    """Return expected profit per dollar committed, per year.

    `net_edge * units` is the whole position's expected profit in USD;
    `capital_lockup_usd` is what it takes to hold it. Their ratio is the
    return, and it is what makes two strategies' `composite` values mean
    the same thing (module docstring, point 3) — PLAN.md D10's literal
    formula divided by time but never by capital, which ranked a $0.02
    edge on a $0.98 pair level with the same $0.02 edge on a $0.20 leg.

    See the module docstring's "THE ANNUALIZATION FLOOR". Below the
    floor, `hours_to_resolution` is treated AS IF it were the floor for
    THIS calculation only — `OpportunityScore.hours_to_resolution` itself
    is reported unfloored elsewhere.

    Args:
        net_edge: Per-unit edge after every haircut, USD.
        units: `_units(intent)` — contracts of every leg.
        capital_lockup_usd: `_capital_lockup_usd(intent)`.
        hours_to_resolution: Unfloored hours to resolution.
        settings: Supplies `min_hours_for_annualization`.

    Returns:
        float: `(net_edge * units / capital_lockup_usd) / max(hours,
            min_hours) * 8760`, or `0.0` when no capital is committed —
            an intent that locks up nothing has no return per dollar to
            report, and `net_edge` still carries the raw edge.
    """
    if capital_lockup_usd <= _PRICE_EPSILON:
        return 0.0
    floor_hours = max(hours_to_resolution, settings.min_hours_for_annualization)
    return ((net_edge * units) / capital_lockup_usd / floor_hours) * HOURS_PER_YEAR


def _leg_contracts(leg: Leg) -> float:
    """Return the contracts requested by one leg, or `0.0` if unsized.

    Args:
        leg: The leg to size.

    Returns:
        float: `leg.size_contracts` if set; `leg.size_usd / limit_price`
            if only the dollar size is set (and the limit is positive);
            `0.0` for an entirely unsized leg.
    """
    if leg.size_contracts is not None:
        return leg.size_contracts
    if leg.size_usd is not None and leg.limit_price > _PRICE_EPSILON:
        return leg.size_usd / leg.limit_price
    return 0.0


def _capital_lockup_usd(intent: Intent) -> float:
    """Return total USD notional committed across every leg.

    Args:
        intent: The intent being scored.

    Returns:
        float: `sum(leg.limit_price * leg.size_contracts)` for a
            contract-sized leg, `leg.size_usd` for a dollar-sized one; an
            entirely unsized leg contributes `0.0`.
    """
    total = 0.0
    for leg in intent.legs:
        if leg.size_contracts is not None:
            total += leg.limit_price * leg.size_contracts
        elif leg.size_usd is not None:
            total += leg.size_usd
    return total


def _is_crossed(book: OrderBook) -> bool:
    """Return whether `book` is CROSSED (`best_bid > best_ask`), not merely locked.

    Mirrors `app.execution.fill_engine`'s own crossed-book definition
    (duplicated rather than imported — see `_PRICE_EPSILON`'s comment).
    A crossed book declines to fill there with `reason="crossed_book"`;
    the same book must not be treated as fillable here either (PLAN.md
    R4 / this module's carry-forward from T07).

    Args:
        book: The book to check.

    Returns:
        bool: `True` if this book must not be walked for a fill.
    """
    if book.metadata.get(_CROSSED_QUOTES_KEY) is True:
        return True
    bid = book.best_bid()
    ask = book.best_ask()
    if bid is None or ask is None:
        return False
    return bid.price > ask.price + _PRICE_EPSILON


def _fill_confidence(intent: Intent, ctx: ScoreContext) -> float:
    """Return the minimum, across legs, of contracts fillable / requested.

    "Fillable" means: walking `leg`'s book (`OrderBook.walk` — the
    repo's one depth primitive, never a second one written here) from
    the best price, counting only the CONSECUTIVE best-first fills whose
    price is within `settings.max_slippage_bps` of the leg's limit (BUY:
    at or below `limit * (1 + bps/1e4)`; SELL: at or above `limit * (1 -
    bps/1e4)`) — the walk is stopped at the first level outside that
    bound rather than skipping it, since `OrderBook.walk` returns levels
    best-price-first and a later level is never a better price than one
    already rejected.

    A leg with nothing requested (`_leg_contracts(leg) <= 0`) is left
    out of the aggregation entirely — there is nothing to judge
    fillable. An intent with NO sized legs at all (every leg empty)
    scores `0.0`: an unsized intent is not "perfectly fillable", it is
    unjudged, and treating it as `1.0` would let it outrank a genuinely
    thick, fully-sized opportunity.

    Args:
        intent: The intent being scored.
        ctx: Supplies `books` and `settings.max_slippage_bps`.

    Returns:
        float: In `[0.0, 1.0]`.
    """
    slippage = float(ctx.settings.max_slippage_bps) / 10_000.0
    per_leg: list[float] = []
    for leg in intent.legs:
        requested = _leg_contracts(leg)
        if requested <= _PRICE_EPSILON:
            continue
        book = ctx.books.get((leg.venue, leg.market_id, leg.outcome))
        if book is None or _is_crossed(book):
            per_leg.append(0.0)
            continue
        if leg.side == "BUY":
            bound = leg.limit_price * (1.0 + slippage)
            walk_side: WalkSide = "buy"
        else:
            bound = leg.limit_price * (1.0 - slippage)
            walk_side = "sell"
        filled = 0.0
        for price, qty in book.walk(walk_side, requested):
            within_bound = (
                price <= bound + _PRICE_EPSILON
                if leg.side == "BUY"
                else price >= bound - _PRICE_EPSILON
            )
            if not within_bound:
                break
            filled += qty
        per_leg.append(min(1.0, filled / requested))
    if not per_leg:
        return 0.0
    return min(per_leg)


def _resolution_risk(
    leg_markets: list[VenueMarket], hours_to_resolution: float
) -> float:
    """Return `resolution_risk` in `[0.0, 1.0]` (PLAN.md D10).

    SETTLEMENT risk only — the chance a venue mis-adjudicates, delays or
    disputes a question this intent's legs redeem off. IDENTITY risk (the
    chance two markets are not the same event at all) is NOT here: it is
    priced in dollars by `_risk_adjusted_edge`, once. Pricing it in both
    places is the T31 defect — see the module docstring's point 2.

    The base term compounds over DISTINCT `(venue, market_id)` pairs, not
    over legs: a bundle's five legs redeem off one question and carry one
    adjudication risk; a cross-venue pair's two legs carry two. This is
    the only term that treats a cross-venue intent differently from a
    same-venue one, and `1 - (1 - 0.15)**2 = 0.2775` is derived from
    `_RESOLUTION_RISK_BASE`'s own stated meaning rather than chosen.

    `intent` is deliberately NOT a parameter any more: every input is a
    property of the MARKETS the legs settle on, and reading
    `intent.confidence` here was how the second identity discount got in.

    Args:
        leg_markets: One `VenueMarket` per leg, from `_leg_markets`
            (duplicated for legs sharing a market — de-duplicated here).
        hours_to_resolution: Unfloored hours to resolution.

    Returns:
        float: In `[0.0, 1.0]`.
    """
    distinct_markets = len({(market.venue, market.market_id) for market in leg_markets})
    risk = 1.0 - (1.0 - _RESOLUTION_RISK_BASE) ** distinct_markets
    if any(len(market.rules_text) < _RULES_TEXT_MIN_CHARS for market in leg_markets):
        risk += _RULES_TEXT_PENALTY
    if any(market.resolution_source is None for market in leg_markets):
        risk += _NO_RESOLUTION_SOURCE_PENALTY
    if hours_to_resolution < _DISPUTE_WINDOW_HOURS:
        risk += _DISPUTE_WINDOW_PENALTY
    return min(_RESOLUTION_RISK_CAP, risk)


def _depth_source(intent: Intent, ctx: ScoreContext) -> str:
    """Return the aggregate `OrderBook.depth_source` across every leg's book.

    Mirrors `app.services.backtesting.engine.Backtester._aggregate_depth_source`'s
    three-way convention (duplicated, not imported — that method is
    private to the backtest engine and this module scores live/paper
    intents, not backtest fills): `"recorded"` only if every leg's book
    was found and recorded; `"mixed"` if both recorded and synthetic
    books were seen; `"synthetic"` — the conservative default — if NO
    leg's book could be found at all (GUARDRAILS.md §1.7: labeling a
    result `"recorded"` on the strength of zero recorded books is
    exactly the hidden assumption that section forbids).

    Args:
        intent: The intent being scored.
        ctx: Supplies `books`.

    Returns:
        str: `"recorded"`, `"synthetic"`, or `"mixed"`.
    """
    sources: set[str] = set()
    for leg in intent.legs:
        book = ctx.books.get((leg.venue, leg.market_id, leg.outcome))
        if book is not None:
            sources.add(book.depth_source)
    if len(sources) > 1:
        return "mixed"
    if sources == {"recorded"}:
        return "recorded"
    return "synthetic"
