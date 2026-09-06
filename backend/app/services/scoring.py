"""Opportunity scoring (PLAN.md D10, T19).

`score(intent, ctx)` is the ONE place every strategy's output is reduced
to a single, comparable `composite` ranking number, regardless of which
strategy produced it or which venue(s) it touches. It is deliberately
generic over `Intent.kind`: `binary_complement_arbitrage` stamps its
per-contract edge under `metadata["edge"]`,
`cross_venue_arbitrage` (T18) under `metadata["net_edge"]`, and a plain
`Signal.to_intent()` stamps neither — `score()` reads whichever key a
strategy actually published and falls back to `0.0` (no measurable
riskless edge) rather than inventing one a strategy never computed.

THE FORMULA (PLAN.md D10, verbatim)::

    annualized_return = net_edge / max(hours_to_resolution, min_hours) * 8760
    fill_confidence    = min over legs of (contracts fillable within
                          max_slippage_bps of the leg's limit) / requested
    resolution_risk    = 0.15 base
                         + 0.25 if any leg's market has rules_text < 200 chars
                         + 0.20 if any leg's market has resolution_source is None
                         + 0.25 if kind == "cross_venue" and confidence < 0.95
                         + 0.15 if hours_to_resolution < 6  (dispute window)
                         , capped at 1.0
    composite          = annualized_return * fill_confidence * (1 - resolution_risk)

WHY THE CROSS-VENUE TERM IS THE MOST CONSEQUENTIAL NUMBER HERE — MEASURED,
NOT A FUDGE FACTOR. Same-venue complement arbitrage needs roughly a 2.5%
gross edge to clear Polymarket's taker fee at mid-range prices; cross-venue
is far harder because BOTH legs must actually settle on the same fact.
Measured against this repo's own fee models and PLAN.md D8's arithmetic,
the gross edge required to make a cross-venue pair worth trading AT ALL
is approximately 1.50% at link confidence 1.00, 2.55% at 0.98, 4.21% at
0.95, 7.22% at 0.90, and 14.37% at 0.80 — because a resolution mismatch
does not cost the gross edge, it costs the LOSING LEG'S ENTIRE STAKE
(`app.strategies.cross_venue_arbitrage`'s worked example: Polymarket YES
0.46 + Kalshi NO 0.50 nets +1.05% gross, +1.05%*p at p=1.00, but -1.5% at
p=0.95 and -9.2% at p=0.80 once the haircut is applied). `0.95` is where
that asymmetry turns sharply negative for realistic gross edges, which is
why `resolution_risk` adds a flat 0.25 there rather than scaling smoothly
with confidence — below 0.95 the trade is usually negative-EV outright,
not merely "riskier".

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
from app.strategies.base import Intent, Leg
from app.venues.types import OrderBook, VenueMarket, WalkSide

#: Hours in a 365-day year — the same annualization constant
#: `app.strategies.cross_venue_arbitrage.HOURS_PER_YEAR` uses. Not a fee,
#: not a rate: a unit conversion, kept in sync by hand rather than
#: imported (scoring must not depend on any one strategy module).
HOURS_PER_YEAR = 8760.0

#: `resolution_risk`'s unconditional floor (PLAN.md D10) — every
#: hold-to-resolution intent carries SOME settlement risk (a venue can
#: mis-adjudicate, delay, or dispute a resolution) even with perfect
#: documentation and a certain link.
_RESOLUTION_RISK_BASE = 0.15

#: Below this many characters, a market's `rules_text` is too thin to
#: adjudicate an edge case with confidence (PLAN.md D10).
_RULES_TEXT_MIN_CHARS = 200
_RULES_TEXT_PENALTY = 0.25

#: No named resolution authority at all.
_NO_RESOLUTION_SOURCE_PENALTY = 0.20

#: See the module docstring's "WHY THE CROSS-VENUE TERM..." section —
#: this is a measured breakpoint, not a round number chosen for looks.
_CROSS_VENUE_CONFIDENCE_FLOOR = 0.95
_CROSS_VENUE_PENALTY = 0.25

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
        net_edge: Per-contract riskless edge, USD, already net of fees,
            gas, and (for a cross-venue intent) the resolution-mismatch
            haircut — whatever the ORIGINATING STRATEGY computed and
            published in `Intent.metadata` (`"net_edge"` or `"edge"`);
            `0.0` if the strategy published neither (a non-arbitrage
            intent has no riskless edge for this field to report).
        annualized_return: `net_edge` expressed as a fraction of capital
            per year, floored per `settings.min_hours_for_annualization`
            — see the module docstring's "THE ANNUALIZATION FLOOR".
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
            any leg's market is already `"resolved"`, or (unless
            `allow_past_close`) if any leg's market is past its
            `close_time`.
    """
    leg_markets = _leg_markets(intent, ctx, allow_past_close=allow_past_close)
    hours_to_resolution = _hours_to_resolution(intent, ctx.now)
    net_edge = _net_edge(intent)
    annualized_return = _annualized_return(net_edge, hours_to_resolution, ctx.settings)
    fill_confidence = _fill_confidence(intent, ctx)
    resolution_risk = _resolution_risk(intent, leg_markets, hours_to_resolution)
    capital_lockup_usd = _capital_lockup_usd(intent)
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


def _net_edge(intent: Intent) -> float:
    """Return the strategy-published per-contract net edge, or `0.0`.

    Reads whichever key the ORIGINATING strategy actually published —
    `"net_edge"` (`app.strategies.cross_venue_arbitrage`, already net of
    the resolution-mismatch haircut) takes priority over `"edge"`
    (`app.strategies.binary_complement_arbitrage`). Scoring never
    RECOMPUTES an edge from fees/books itself: doing so would require
    guessing which fee schedule and haircut a given `Intent.kind` needs,
    which is exactly the strategy-specific logic PLAN.md D8 already
    lives in. An intent kind that publishes neither key (a directional
    single-leg intent) has no measurable riskless edge for this field.

    Args:
        intent: The intent being scored.

    Returns:
        float: The published net edge, or `0.0`.
    """
    if "net_edge" in intent.metadata:
        return float(intent.metadata["net_edge"])
    if "edge" in intent.metadata:
        return float(intent.metadata["edge"])
    return 0.0


def _annualized_return(net_edge: float, hours_to_resolution: float, settings: Settings) -> float:
    """Return `net_edge` annualized, floored at `settings.min_hours_for_annualization`.

    See the module docstring's "THE ANNUALIZATION FLOOR". Below the
    floor, `hours_to_resolution` is treated AS IF it were the floor for
    THIS calculation only — `OpportunityScore.hours_to_resolution` itself
    is reported unfloored elsewhere.

    Args:
        net_edge: Per-contract net edge, USD.
        hours_to_resolution: Unfloored hours to resolution.
        settings: Supplies `min_hours_for_annualization`.

    Returns:
        float: `net_edge / max(hours_to_resolution, min_hours) * 8760`.
    """
    floor_hours = max(hours_to_resolution, settings.min_hours_for_annualization)
    return (net_edge / floor_hours) * HOURS_PER_YEAR


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
    intent: Intent, leg_markets: list[VenueMarket], hours_to_resolution: float
) -> float:
    """Return `resolution_risk` in `[0.0, 1.0]` (PLAN.md D10).

    See the module docstring's formula and its "WHY THE CROSS-VENUE
    TERM..." explanation. `intent.confidence` is used verbatim as the
    cross-venue link's `p_same_resolution` — `app.strategies
    .cross_venue_arbitrage._build_intent` stamps
    `Intent.confidence = evaluation.p_same_resolution` for exactly this
    reason, so no separate link lookup is needed here.

    Args:
        intent: The intent being scored.
        leg_markets: One `VenueMarket` per leg, from `_leg_markets`.
        hours_to_resolution: Unfloored hours to resolution.

    Returns:
        float: In `[0.0, 1.0]`.
    """
    risk = _RESOLUTION_RISK_BASE
    if any(len(market.rules_text) < _RULES_TEXT_MIN_CHARS for market in leg_markets):
        risk += _RULES_TEXT_PENALTY
    if any(market.resolution_source is None for market in leg_markets):
        risk += _NO_RESOLUTION_SOURCE_PENALTY
    if intent.kind == "cross_venue" and intent.confidence < _CROSS_VENUE_CONFIDENCE_FLOOR:
        risk += _CROSS_VENUE_PENALTY
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
