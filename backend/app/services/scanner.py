"""The opportunity scanner: discovery only, NEVER routing (PLAN.md D10, T19).

`scan()` reads open markets and their books from read-only adapters, runs
every requested strategy's `on_market_data` over them, scores every
`Intent` a strategy returns (`app.services.scoring.score`), and persists
each as a `pending` `app.models.intent.IntentRecord`. That is the entire
contract. THIS MODULE DOES NOT IMPORT `app.execution.router.OrderRouter`
AND NEVER ROUTES AN ORDER — not conditionally, not behind a flag, not in
a code path this file's own tests don't reach.

WHY THAT SEPARATION IS STRUCTURAL, NOT A STYLE CHOICE. This scanner is
meant to run on a 120-second Celery beat (`app.tasks.scanner
.scan_opportunities`) over LIVE venue data. If discovery could also
route, a scoring bug — a wrong sign, a stale book, an off-by-one in a
strategy's edge formula — would become an automatic trading bug at
machine speed, unreviewed, every two minutes. Routing an `Intent` this
module discovers is a SEPARATE, EXPLICIT call
(`app.execution.router.OrderRouter.submit`) made by something else
entirely (an operator via the API, or a future bot), with a human or an
explicit process in between. `tests/test_fences.py`-style AST checks are
what GUARDRAILS.md §1.1 uses for order placement; this module's own
discipline is the equivalent for order ROUTING: no import, no call, no
seam that could grow one by accident.

ADAPTERS COME FROM `get_read_adapter`, NEVER `get_adapter`. This module
does not call either directly — `adapters` is a parameter, supplied by
the caller (`app.api.routes.arbitrage`'s `POST /scan`,
`app.tasks.scanner.scan_opportunities`), both of which use
`app.venues.registry.get_read_adapter`/`app.api.deps
.get_market_data_adapters` — so a read-only scan can never accidentally
acquire an order-placing adapter (GUARDRAILS.md §1.1; see
`app.venues.base.MarketDataAdapter`'s docstring for why a read adapter
structurally cannot place one).

VOLUME RANKING. `VenueMarket` has no normalized `volume_24h` field yet
(only `app.strategies.base.MarketSnapshot` does, built from a venue's
OWN payload). Both real venues' raw payloads carry a `"volume"` key
(`tests/fixtures/polymarket/gamma_markets.json`,
`tests/fixtures/kalshi/markets.json`), so `_volume()` reads
`VenueMarket.raw["volume"]` as the best available proxy for "top-N
markets by 24h volume" (`settings.scan_top_n`, default 200 per venue)
rather than inventing a new normalized field for this one task.

BOOK FETCH IS CONCURRENT, BOUNDED, AND PER-CALL ISOLATED (T35). Naively,
`scan_top_n` (200) x 2 outcomes x 2 venues is up to 800 `get_book`
calls a pass, and awaiting them one at a time is two problems, not one.
The obvious one is rate-limit risk against request budgets now shared by
THREE beats (`scan_opportunities`, `scan_near_resolution`,
`propose_event_links`). The one that actually bites is SNAPSHOT SKEW: a
cross-venue arbitrage signal is a claim that two prices are inconsistent
AT THE SAME MOMENT, and if walking 800 books serially takes minutes, the
book fed to the strategy for venue A's leg can be three minutes stale
relative to venue B's — at which point an "edge" can be entirely an
artifact of the clock, not a real mispricing. `scan()` therefore fetches
every `(venue, market, outcome)` book THIS PASS NEEDS, from EVERY venue
TOGETHER, under ONE `asyncio.Semaphore` sized `settings.
scan_book_fetch_concurrency` (a bound, not a literal — see that field's
comment) — one shared semaphore across venues, not one per venue,
because a per-venue bound would still let one venue's 200 books finish
long before the other venue's have even started, which does nothing for
skew (see PLAN's own concern: a cross-venue pair is exactly two
DIFFERENT venues' books). A SHARED semaphore alone is not sufficient,
either: `asyncio.Semaphore` queues blocked waiters FIFO in the order
they first tried to acquire, i.e. the order `asyncio.gather`'s
coroutines were listed in — so a naive venue-by-venue spec list (every
one of venue A's specs before venue B's first) would still let venue A
monopolize the front of that queue and push venue B's books into the
back half of the pass, reproducing the same sequential-by-venue skew
one layer down. `_interleave_fetch_specs` round-robins across venues
for exactly this reason: every batch the semaphore admits contains a
mix of venues from the very first one, so both venues' books arrive
throughout the WHOLE fetch window instead of in two back-to-back
blocks. `_fetch_book` is the unit of work `asyncio.
gather` runs 800(-ish) copies of; it catches `VenueError` INSIDE itself
and returns `None` rather than raising, which is the part that matters —
`asyncio.gather`'s default `return_exceptions=False` cancels every OTHER
in-flight fetch the instant any one coroutine raises, so catching
outside the gather (or not at all) would turn "Kalshi's book 47 is a
404" into "every book this pass was fetching, from BOTH venues, is lost"
— a strictly worse failure than the serial code this replaces, where one
bad book only cost that one book. Catching inside each unit of work is
what makes the serial code's per-book isolation (unchanged: still
exactly one `except VenueError`, still logged at `debug`, still skipped
without effect on any other book) survive the move to concurrency, and
it is also what gives a wholly-unreachable venue (not just one bad
market) the same treatment for free — its books all resolve to `None`
while the other venue's arrive on schedule, with no separate branch
needed. `fetch_elapsed_s` (logged as `scan_book_fetch_complete`) is the
wall-clock length of that ENTIRE concurrent phase — the cheapest number
that answers "were this pass's books close enough together in time for
its own signals to mean anything," without adding a persisted
per-book-pair skew table (a real measurement, but a schema/API change
outside this task's file set, and overkill for what is fundamentally an
operator health signal, not a scored input). This is a fetch-STRATEGY
change only: the two-phase split (fetch every book first, THEN build
every `MarketSnapshot`) preserves the exact venue/market iteration order
`scan()` always built `snapshots` in, so which opportunities a given
fixture yields is unchanged — only WHEN, and in what order, the network
calls that fill `books_by_key` complete is different, and nothing here
reads that order.

LINK STATUS RIDES THROUGH TO THE SCORE. `app.strategies
.cross_venue_arbitrage.LinkBook` accepts ONLY `status == "approved"`
links (PLAN.md D9) — `_build_strategies` below filters `links` to
`"approved"` before handing them to `CrossVenueArbitrageStrategy`, so
nothing this scanner runs can ever be handed a non-approved link. Every
cross-venue intent this scan produces is therefore attributable to an
APPROVED link, and `_stamp_link_status` looks that link back up (by the
`link_id` the strategy already publishes in `metadata`) and stamps
`metadata["link_status"]` accordingly, so `app.services.scoring.score`'s
`OpportunityScore.link_status` carries a real, non-fabricated value all
the way to `/arbitrage/opportunities` (GUARDRAILS.md §1.7's labeling
discipline, applied to link provenance rather than depth).

`near_resolution_pass()` (T20, PLAN.md D10(a)-(d)) IS A SEPARATE PASS,
NOT A FILTER OVER `scan()`, AND IT HAS ITS OWN PRODUCTION CALLERS (T25):
the `scan-near-resolution` Celery beat entry
(`app.tasks.scanner.scan_near_resolution`, every
`settings.near_resolution_scan_interval_s`) and `POST
/arbitrage/scan/near-resolution`. Until T25 it had NEITHER — no beat
entry, no route, no caller anywhere in `app/` — so the whole bucket
apparatus built on the `metadata["bucket"] = "near_resolution"` tag it
alone produces (`check_order_limits`'s bucket cap,
`OrderRouter._bucket_open_notional`, the `Position.extra_data
["bucket_notional"]` ledger) guarded a path nothing walked, and
`GET /arbitrage/opportunities?near_resolution=true` returned an empty
list that read as "no near-resolution edges right now" when in truth no
code path could produce one. `scan()` cannot substitute for it in
either direction: `settlement_edge` is in `STRATEGY_CATEGORIES["edge"]`,
so `ARBITRAGE_STRATEGIES` never runs it, and even when named explicitly
`scan()` scores WITHOUT `allow_past_close=True`, which (see the
`close_time` paragraph below) drops 100% of settlement-edge intents
silently. `app.api.routes.arbitrage.trigger_scan` therefore REFUSES a
`strategies=settlement_edge` request outright rather than answering it
with an empty, truthful-looking list.

It exists for exactly one strategy,
`app.strategies.settlement_edge.SettlementEdgeStrategy` — the
capital-lockup, "outcome already determined" trade PLAN.md D10(c)
describes (see that module's docstring for why this is settlement risk,
not an information edge, and never sized like one). This is a
deliberate scope decision, not an oversight: the ordinary arbitrage
strategies `scan()` already runs are perfectly well scored by the
GENERAL path for any near-resolution market that is still actually
open (small `hours_to_resolution`, `close_time` still ahead); it is
ONLY the "outcome determined" / dispute-window regime — where
`close_time` has ALREADY passed — that the general path cannot score at
all (see next paragraph), and `settlement_edge` is the one strategy
PLAN.md ties to that regime.

THE `close_time`-PASSED TENSION, AND HOW IT IS RESOLVED. `outcome_determined`
(PLAN.md D10(c)) is defined as "price >= 0.97 or <= 0.03 WITH close_time
passed" — by construction, every intent this pass can emit is on a
market whose `close_time` is already behind us. But
`app.services.scoring.score()` (T19) raises `UnscorableIntent` for
EXACTLY that market shape (`close_time <= now`), because T19's rule was
written for the general case: an ordinary strategy has no business
still quoting a market whose trading window is closed. Left alone, this
pass would generate every settlement-edge intent and then have every
single one of them silently dropped by the score() call `scan()` uses —
a strategy that looks wired up but produces zero rows, forever. The fix
is `score(..., allow_past_close=True)` (T20 addition to `scoring.py`):
it skips ONLY the `close_time` half of that guard, on THIS call site
alone, while leaving the `"resolved"` half — the actual T09 rule, "no
fill can occur on a resolved market" — fully in force. A genuinely
`status == "resolved"` market is still refused here exactly as `scan()`
refuses it; only "closed but not yet finalized" (Polymarket's own
payload commonly reports `status="open"` straight through the UMA
challenge window — `close_time` there is Gamma's `endDate`, a schedule,
not a live/closed flag) becomes scorable, and only for this one caller.

WHY THIS PASS BUILDS ITS OWN `OpportunityScore` ON TOP OF `score()`,
RATHER THAN PERSISTING `score()`'S OUTPUT VERBATIM. Two things are
bucket-specific and PLAN.md D10 requires them on TOP of the generic
formula, not instead of it:

  1. `annualized_return` is the STRATEGY-PUBLISHED figure
     (`intent.metadata["annualized_return"]`), not `score()`'s own.
     `score()`'s generic formula is `net_edge / floor_hours *
     HOURS_PER_YEAR` — it never divides by the capital actually
     deployed at all, which is an adequate approximation for
     `binary_complement_arbitrage`/`cross_venue_arbitrage` (their
     combined ask sums are close to $1.00, so a per-contract dollar
     edge and a fractional return are numerically close) but wrong
     here, where a near-certain ask genuinely varies across `(0, 1)`
     and PLAN.md D10(c)'s own formula is explicitly capital-normalized:
     `(1 - ask - fee - gas) / ask`. `SettlementEdgeStrategy.evaluate`
     already computes that correctly, floored at
     `max(hours_to_resolution, settings.settlement_delay_hours)` — the
     REAL capital lockup, you do not get paid the moment `close_time`
     passes, you get paid after the venue actually settles, and using
     the smaller, generic `min_hours_for_annualization` floor `score()`
     itself uses would OVERSTATE the return for any market whose
     settlement estimate is under `settlement_delay_hours` away (see
     `app.strategies.settlement_edge`'s docstring for the
     0.98-vs-0.995 worked arithmetic that depends on this floor). This
     pass reads that already-correct number back off the intent rather
     than re-deriving a different, capital-blind one from
     `score()`'s own `net_edge`.
  2. `resolution_risk` gets two ADDITIONAL penalties `score()` has no way
     to know about — `+0.3` when `in_dispute_window`, `+0.2` when
     `liquidity_collapse` — stacked on top of whatever `score()` already
     computed (its own generic `< 6h` dispute-window proxy included; the
     two signals measure different things and are not deduplicated),
     capped at `1.0` the same way `score()` caps its own formula.

Everything else — `fill_confidence`, `depth_source`, `net_edge`,
`capital_lockup_usd`, `hours_to_resolution`, `link_status` — is read
straight from `score()`'s result and NOT recomputed, so this pass never
duplicates that math.

THE THREE MARKET-LEVEL SIGNALS, computed HERE (not by the strategy,
which only ever sees a bare `MarketSnapshot` — see
`SettlementEdgeStrategy.evaluate`'s docstring for why):

  - `in_dispute_window` (PLAN.md D10(b)): Polymarket keys off
    `market.close_time`; Kalshi keys off `market.expected_settle_time`
    (its `expected_expiration_time`, which is chronologically AFTER
    `close_time` — trading stops, THEN the settlement timer runs) —
    both "AND `status != 'resolved'`". A Kalshi market can therefore have
    `close_time` passed (eligible for `outcome_determined`) while its
    OWN, later `expected_settle_time` has not — genuinely near
    resolution, not yet in the window that is actually disputed.
  - `liquidity_collapse` (PLAN.md D10(a)): `spread_now / median(spread
    over the trailing 24h of `PriceHistory` rows for this
    `market_id`)` `> 3`. AN ABSENT HISTORY IS NOT A CLEAN BILL OF
    HEALTH: when there is no trailing spread to compare against (a
    market this repo has not been collecting `PriceHistory` for, e.g.
    pre-T21 `book_snapshots`), the ratio defaults to `1.0`
    (`liquidity_collapse=False`) ONLY because there is nothing else to
    compute, and `intent.metadata["liquidity_collapse_unavailable"]` is
    stamped `True` alongside it — a caller (a future dashboard, a
    reviewer) must read THAT flag before reading `liquidity_collapse` as
    "measured fine", never conflate "unmeasured" with "measured safe".
  - The settlement-time ESTIMATE fed to `score()`/the strategy as
    `expected_resolution_ts`: `market.expected_settle_time` when the
    venue states one (Kalshi), else `market.close_time +
    settings.settlement_delay_hours` (Polymarket, whose adapter never
    populates `expected_settle_time` — PLAN.md's own
    `settlement_delay_hours` IS this assumed gap, used here as the
    estimate for the one venue that does not tell us, and ALSO as the
    annualization floor above for the venue that does but might state
    something implausibly soon).

Market SELECTION reads every status (`adapter.list_markets(status=None)`),
not just `"open"` (`scan()`'s filter) — Kalshi's own payload moves a
closed-but-undetermined market to `status="closed"` (see
`app/venues/kalshi/adapter.py::_MARKET_STATUS_FROM_PAYLOAD`), which
`scan()`'s `status="open"` filter would silently exclude from EVERY
pass. A market is then kept if `status != "resolved"` and
`(market.close_time - now)` in hours is `<= settings.near_resolution_hours`
— a market whose `close_time` is already behind us has a NEGATIVE value
here, which trivially satisfies "<= 72" and is exactly the
dispute-window/outcome-determined regime this pass exists for; no
separate branch is needed for "already closed" vs. "closing soon", one
comparison covers both.

STRUCTURALLY READ-ONLY, same guarantee as `scan()` (see this module's
opening paragraphs): this function takes the same `MarketDataAdapter`
mapping, never imports `OrderRouter`, and never calls `.submit()`.

THE TWO PASSES SHARE ONE OPPORTUNITIES LIST WITHOUT HIDING EACH OTHER.
Each mints its own `scan_id` and runs on its own clock, so every
persisted row also carries `extra_data[SCAN_PASS_KEY]` naming which
pass wrote it, and `GET /arbitrage/opportunities` keeps the newest
`scan_id` PER PASS. See `SCAN_PASS_KEY` for why a single global "latest
scan" would have made the two surfaces mutually exclusive.
"""
import asyncio
import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from itertools import zip_longest
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.config import settings as default_settings
from app.models.event_link import EventLink
from app.models.intent import IntentRecord
from app.models.price_history import PriceHistory
from app.services.scoring import (
    OpportunityScore,
    ScoreContext,
    UnscorableIntent,
    score,
)
from app.strategies import STRATEGY_CATEGORIES, get_strategy
from app.strategies.base import (
    BaseStrategy,
    Intent,
    Leg,
    MarketSnapshot,
    normalize_outcome,
)
from app.strategies.cross_venue_arbitrage import CrossVenueArbitrageStrategy
from app.strategies.settlement_edge import SettlementEdgeStrategy
from app.utils.time import utcnow
from app.venues.base import MarketDataAdapter, VenueError
from app.venues.types import BookLevel, OrderBook, VenueId, VenueMarket

logger = logging.getLogger(__name__)

#: The strategies `POST /arbitrage/scan` and `scan_opportunities` run
#: when the caller does not name a specific set — the same three keys
#: `app.strategies.STRATEGY_CATEGORIES["arbitrage"]` declares, re-read
#: here (not duplicated by hand) so a fourth arbitrage strategy is
#: picked up automatically.
ARBITRAGE_STRATEGIES: tuple[str, ...] = tuple(STRATEGY_CATEGORIES["arbitrage"])

#: The one strategy `near_resolution_pass()` runs, and the one strategy
#: `scan()` CANNOT run: `scan()` scores without `allow_past_close=True`,
#: so every intent this strategy produces (all of them on a market whose
#: `close_time` has passed, by construction) is silently dropped there.
#: Named here so `app.api.routes.arbitrage` can reject a request that
#: asks the general pass for it, rather than answering with an empty list.
NEAR_RESOLUTION_STRATEGY: str = SettlementEdgeStrategy.name

#: `IntentRecord.extra_data` key naming WHICH PASS produced a row, and
#: the two values it takes (T25). Every persisted scan row carries one.
#:
#: `scan()` and `near_resolution_pass()` each mint their OWN `scan_id`,
#: and `app.api.routes.arbitrage.list_opportunities` shows "the latest
#: scan". With one global newest-`scan_id` that made the two passes
#: MUTUALLY EXCLUSIVE by construction: whichever ran second erased the
#: other from the list, so wiring the near-resolution beat up would have
#: silently switched the arbitrage opportunities off every 300 seconds
#: (and back on 120 seconds later). Labelling the pass lets that route
#: keep the newest `scan_id` PER PASS instead — each pass replaces only
#: its own previous results, which is what "latest scan" means when
#: there is more than one scanner on more than one clock.
#:
#: This is a label for the PRODUCING PASS, deliberately not a re-derived
#: view of the row's contents. It is not the same thing as
#: `metadata["bucket"]` (which `SettlementEdgeStrategy` stamps per
#: INTENT, drives the `max_near_resolution_notional_usd` cap, and is
#: absent from every arbitrage intent), and not the same thing as
#: `strategy` (one pass runs several).
SCAN_PASS_KEY = "scan_pass"
ARBITRAGE_SCAN_PASS = "arbitrage"
NEAR_RESOLUTION_SCAN_PASS = "near_resolution"


@dataclass(frozen=True)
class ScoredIntent:
    """One strategy's `Intent`, scored and persisted by one `scan()` call.

    Attributes:
        intent: The intent as the strategy produced it (legs, metadata,
            `expected_resolution_ts` all as emitted — `metadata` may have
            gained a `"link_status"` key, see `_stamp_link_status`).
        score: Its `OpportunityScore`.
        strategy: The strategy name that produced it (a `STRATEGIES` key).
        intent_record_id: Primary key of the persisted `IntentRecord`
            row. A UUID4 minted HERE, never reused as an order id — order
            ids are minted only at routing time
            (`app.execution.router.OrderRouter`), a separate call this
            module never makes.
    """

    intent: Intent
    score: OpportunityScore
    strategy: str
    intent_record_id: str


def _volume(market: VenueMarket) -> float:
    """Return the best available 24h-volume proxy for ranking, or `0.0`.

    See the module docstring's "VOLUME RANKING" section.

    Args:
        market: The market to rank.

    Returns:
        float: `market.raw["volume"]` coerced to `float`; `0.0` if the
            key is absent or not numeric.
    """
    raw_volume = market.raw.get("volume")
    try:
        return float(raw_volume)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _level_price(level: BookLevel | None) -> float | None:
    """Return `level.price`, or `None` if there is no level."""
    return level.price if level is not None else None


def _snapshot_from_market(
    market: VenueMarket,
    books: Mapping[tuple[str, str, str], OrderBook],
    now: datetime,
) -> MarketSnapshot:
    """Build a `MarketSnapshot` for one market from its already-fetched books.

    `MarketSnapshot.book` carries at most ONE outcome's book (a
    limitation `MarketSnapshot` itself has, not introduced here — full
    per-outcome coverage for `cross_venue_arbitrage` is instead fed
    through its `observe_book()` injection point in `scan()`, below).
    `"YES"` is preferred when present, else the market's first declared
    outcome, so a multi-outcome (`kind="bundle"`) market still gets a
    representative book rather than none.

    Args:
        market: The market to snapshot.
        books: Every book fetched this scan, keyed
            `(venue, market_id, outcome)`.
        now: Aware UTC timestamp to stamp on the snapshot.

    Returns:
        MarketSnapshot: Never raises — a market with no book at all
            still produces a snapshot (bid/ask fields `None`,
            `yes_price` falls back to `0.5`), since a strategy may still
            usefully react to `question`/`category`/`end_date` alone.
    """
    venue = market.venue
    canonical_outcomes = [normalize_outcome(outcome) for outcome in market.outcomes]
    primary_outcome = (
        "YES" if "YES" in canonical_outcomes else (canonical_outcomes[0] if canonical_outcomes else "YES")
    )
    primary_book = books.get((venue, market.market_id, primary_outcome))
    yes_book = books.get((venue, market.market_id, "YES"))
    no_book = books.get((venue, market.market_id, "NO"))

    yes_bid = _level_price(yes_book.best_bid()) if yes_book is not None else None
    yes_ask = _level_price(yes_book.best_ask()) if yes_book is not None else None
    no_bid = _level_price(no_book.best_bid()) if no_book is not None else None
    no_ask = _level_price(no_book.best_ask()) if no_book is not None else None

    yes_mid = yes_book.mid() if yes_book is not None else None
    yes_price = yes_mid if yes_mid is not None else 0.5
    no_price = 1.0 - yes_price
    spread = yes_ask - yes_bid if yes_ask is not None and yes_bid is not None else None

    category = market.raw.get("category")
    return MarketSnapshot(
        market_id=market.market_id,
        token_id=market.outcome_ids.get(primary_outcome, market.market_id),
        timestamp=now,
        yes_price=yes_price,
        no_price=no_price,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        spread=spread,
        volume_24h=_volume(market),
        question=market.question,
        category=str(category) if category is not None else None,
        end_date=market.close_time,
        resolution_rules=market.rules_text,
        venue=venue,
        book=primary_book,
    )


def _build_strategies(
    strategy_names: Sequence[str], links: Sequence[EventLink]
) -> dict[str, BaseStrategy]:
    """Instantiate one fresh strategy per name, wiring approved links only.

    `cross_venue_arbitrage` is handed ONLY `status == "approved"` links —
    PLAN.md D9's "nothing trades on a proposed link" is enforced
    structurally by `CrossVenueArbitrageStrategy`/`LinkBook` (it raises
    on anything else), and filtering here means this scanner never even
    attempts to construct it with one, rather than relying on that raise.

    Args:
        strategy_names: Strategy registry keys to run.
        links: Every `EventLink` the caller read, any status.

    Returns:
        dict[str, BaseStrategy]: One fresh instance per name.

    Raises:
        ValueError: If a name is not in `app.strategies.STRATEGIES`
            (propagated from `app.strategies.get_strategy`).
    """
    approved = [link for link in links if link.status == "approved"]
    strategies: dict[str, BaseStrategy] = {}
    for name in strategy_names:
        if name == CrossVenueArbitrageStrategy.name:
            strategies[name] = CrossVenueArbitrageStrategy(links=approved)
        else:
            strategies[name] = get_strategy(name)
    return strategies


def _stamp_link_status(intent: Intent, links_by_id: Mapping[int, EventLink]) -> None:
    """Stamp `intent.metadata["link_status"]` for a cross-venue intent, in place.

    Looks up `metadata["link_id"]` (published by
    `app.strategies.cross_venue_arbitrage.CrossVenueEvaluation.as_metadata`)
    against `links_by_id` and copies that link's CURRENT `status` onto
    the intent. A no-op for any other `Intent.kind`, or if the id is
    absent or unknown (defensive — `_build_strategies` only ever hands
    `cross_venue_arbitrage` approved links, so this should always resolve
    for a real cross-venue intent).

    Args:
        intent: The intent to stamp, mutated in place (`Intent.metadata`
            is a plain, non-frozen `dict`).
        links_by_id: Every link this scan read, keyed by `EventLink.id`.
    """
    if intent.kind != "cross_venue":
        return
    link_id = intent.metadata.get("link_id")
    if link_id is None:
        return
    link = links_by_id.get(link_id)
    if link is not None:
        intent.metadata["link_status"] = link.status


def _interleave_fetch_specs(
    top_markets_by_venue: Mapping[VenueId, Sequence[VenueMarket]],
) -> list[tuple[VenueId, str, str]]:
    """Round-robin every venue's `(market, outcome)` fetch targets together.

    WHY ORDER MATTERS EVEN THOUGH EVERY SPEC GOES THROUGH THE SAME SHARED
    SEMAPHORE. `asyncio.Semaphore` queues blocked waiters FIFO, in the
    order they first tried to acquire — which, for a single `asyncio.
    gather()` call, is the order its coroutines appear in. Grouping one
    venue's ENTIRE fetch list before another's (as a naive `for venue:
    for market: for outcome` build would) means every one of venue A's
    specs reaches the front of that queue before venue B's FIRST spec
    does; with N specs total and a bound of B, venue B's earliest fetch
    then cannot even START until roughly `len(venue A's specs) / B`
    batches have already drained — a global semaphore reproduces
    "fetch venue A fully, then venue B" in miniature, right down to the
    two venues' books landing in two mostly-disjoint time windows. That
    is exactly the cross-venue snapshot skew this task exists to close,
    just measured in fetch batches instead of minutes. Round-robining
    the specs (venue1[0], venue2[0], venue1[1], venue2[1], ...) instead
    means every batch of `B` admitted specs contains a mix of venues
    from the very first one, so both venues' books arrive throughout the
    WHOLE fetch window rather than in sequential blocks.

    Args:
        top_markets_by_venue: Each venue's already-ranked, already-capped
            (`scan_top_n`) market list, in the order `scan()` read them.

    Returns:
        list[tuple[VenueId, str, str]]: Every `(venue, market_id,
            outcome)` this pass needs a book for, ordered round-robin
            across venues (then in-order within a venue). The exact
            fetch order the caller hands to `asyncio.gather` — it has no
            bearing on `books_by_key`'s contents (a dict, unordered) or
            on `snapshots`' order (built separately, from
            `top_markets_by_venue` directly, in Phase 3).
    """
    per_venue_specs: list[list[tuple[VenueId, str, str]]] = [
        [
            (venue, market.market_id, outcome)
            for market in markets
            for outcome in market.outcomes
        ]
        for venue, markets in top_markets_by_venue.items()
    ]
    interleaved: list[tuple[VenueId, str, str]] = []
    for round_specs in zip_longest(*per_venue_specs, fillvalue=None):
        for spec in round_specs:
            if spec is not None:
                interleaved.append(spec)
    return interleaved


async def _fetch_book(
    adapter: MarketDataAdapter,
    venue: VenueId,
    market_id: str,
    outcome: str,
    semaphore: asyncio.Semaphore,
) -> tuple[VenueId, str, str, OrderBook] | None:
    """Fetch one `(venue, market, outcome)` book under a shared bound.

    See the module docstring's "BOOK FETCH IS CONCURRENT..." section.
    Catching `VenueError` HERE, inside the unit of work `asyncio.gather`
    runs hundreds of copies of, rather than around the `gather` call
    itself, is what keeps one bad book from taking the whole pass down:
    `gather`'s default `return_exceptions=False` cancels every OTHER
    still-running fetch (any venue) the instant any single coroutine
    raises, so an uncaught exception here would turn one 404 into every
    book this pass was fetching, from every venue, being lost.

    Args:
        adapter: The book's own venue's read-only adapter.
        venue: The venue id, carried through so the caller can key the
            result without re-deriving it from `adapter`.
        market_id: The market to fetch a book for.
        outcome: The outcome to fetch a book for.
        semaphore: Shared across the WHOLE pass (every venue's fetches
            together, not one semaphore per venue) so the concurrency
            bound actually limits total in-flight requests rather than
            letting N venues each run `scan_book_fetch_concurrency`
            calls at once.

    Returns:
        tuple[VenueId, str, str, OrderBook] | None: `(venue, market_id,
            outcome, book)` on success; `None` if the venue raised
            `VenueError` (rate-limited, 404, ...) for this one book —
            logged at `debug`, exactly as the serial code always did.
    """
    async with semaphore:
        try:
            book = await adapter.get_book(market_id, outcome)
        except VenueError:
            logger.debug(
                "scanner",
                extra={
                    "event": "get_book_failed",
                    "venue": venue,
                    "market_id": market_id,
                    "outcome": outcome,
                },
            )
            return None
    return venue, market_id, outcome, book


def _leg_dict(leg: Leg) -> dict[str, Any]:
    """Return one leg as a JSON-serializable dict, field by field."""
    return {
        "venue": leg.venue,
        "market_id": leg.market_id,
        "outcome": leg.outcome,
        "side": leg.side,
        "limit_price": leg.limit_price,
        "size_contracts": leg.size_contracts,
        "size_usd": leg.size_usd,
    }


def _persist(
    intent: Intent,
    opportunity_score: OpportunityScore,
    strategy_name: str,
    scan_id: str,
    mode: str,
    session: AsyncSession,
    *,
    scan_pass: str,
) -> IntentRecord:
    """Build and stage a `pending` `IntentRecord` for `intent`, field by field.

    Field-by-field construction (never `IntentRecord(**something)`) per
    `app.models.intent`'s mass-assignment warning — `mode` in particular
    comes from `settings.trading_mode`, never from anything a caller
    supplied.

    Args:
        intent: The scored intent (post `_stamp_link_status`).
        opportunity_score: Its `OpportunityScore`.
        strategy_name: The strategy that produced it.
        scan_id: This `scan()` call's id, carried in `extra_data` so a
            caller can group rows from the same pass.
        mode: `settings.trading_mode` — `"paper"` or `"live"`.
        session: Added to, but not committed — `scan()` commits once for
            the whole pass.
        scan_pass: `ARBITRAGE_SCAN_PASS` or `NEAR_RESOLUTION_SCAN_PASS`,
            carried in `extra_data[SCAN_PASS_KEY]` so a reader can tell
            the two passes' rows apart and show BOTH passes' latest
            results at once (see `SCAN_PASS_KEY`).

    Returns:
        IntentRecord: The staged (not yet flushed) row. Its `id` is
            already set (not autoincrement), so it is safe to read
            immediately.
    """
    extra_data: dict[str, Any] = dict(intent.metadata)
    extra_data["scan_id"] = scan_id
    extra_data[SCAN_PASS_KEY] = scan_pass
    extra_data["confidence"] = intent.confidence
    extra_data["hold_to_resolution"] = intent.hold_to_resolution
    extra_data["atomicity"] = intent.atomicity
    extra_data["expected_resolution_ts"] = (
        intent.expected_resolution_ts.isoformat()
        if intent.expected_resolution_ts is not None
        else None
    )
    record = IntentRecord(
        id=str(uuid.uuid4()),
        kind=intent.kind,
        strategy=strategy_name,
        mode=mode,
        status="pending",
        legs=[_leg_dict(leg) for leg in intent.legs],
        score=asdict(opportunity_score),
        extra_data=extra_data,
    )
    session.add(record)
    return record


async def scan(
    strategy_names: Sequence[str],
    adapters: Mapping[VenueId, MarketDataAdapter],
    links: Sequence[EventLink],
    session: AsyncSession,
    *,
    settings_obj: Settings = default_settings,
) -> list[ScoredIntent]:
    """Run one discovery pass over `adapters`: read, score, persist. Never routes.

    For each venue in `adapters`: reads every `status="open"` market and
    keeps the top `settings.scan_top_n` by `_volume`. Every venue's
    remaining `(market, outcome)` books are then fetched TOGETHER,
    concurrently, bounded by one shared `asyncio.Semaphore` sized
    `settings.scan_book_fetch_concurrency` (T35 — see the module
    docstring's "BOOK FETCH IS CONCURRENT..." section for why this is a
    correctness fix, not only a performance one: a cross-venue signal
    compares two books that must actually be close together in time).
    Each `MarketSnapshot` is then built from the completed fetch. Every
    requested strategy then runs `on_market_data` over every snapshot;
    each resulting `Signal`/`Intent` is normalized to an `Intent`, scored
    (`app.services.scoring.score`), and persisted as a `pending`
    `IntentRecord` — UNLESS scoring raises `UnscorableIntent` (a resolved
    market, a missing `expected_resolution_ts`), in which case that one
    intent is skipped and logged, not the whole pass.

    A failure reading one venue, one book, or one strategy raising on one
    snapshot, is logged and skipped rather than aborting the pass — a
    broken Kalshi feed (whether it cannot be listed at all, or just can't
    answer for one book) must not also blank out Polymarket's
    opportunities, and a bug in one strategy must not silence every
    other one.

    Args:
        strategy_names: `app.strategies.STRATEGIES` keys to run.
        adapters: One READ-ONLY adapter per venue
            (`app.venues.base.MarketDataAdapter` — from
            `app.venues.registry.get_read_adapter`/`app.api.deps
            .get_market_data_adapters`, NEVER `get_adapter`, so this can
            never be handed an order-placing adapter).
        links: Every `EventLink` the caller read, any status —
            `_build_strategies` filters to `"approved"` before handing
            any to `cross_venue_arbitrage` (PLAN.md D9).
        session: Persists every scored `IntentRecord`. `scan()` commits
            once at the end of the pass.
        settings_obj: `Settings` to score and rank against. Defaults to
            the process-wide `app.config.settings` singleton; a caller
            (a test) may pass an explicit instance instead.

    Returns:
        list[ScoredIntent]: Every intent this pass scored, sorted by
            `score.composite` descending.
    """
    now = utcnow()
    scan_id = str(uuid.uuid4())
    strategies = _build_strategies(strategy_names, links)

    markets_by_key: dict[tuple[str, str], VenueMarket] = {}
    top_markets_by_venue: dict[VenueId, list[VenueMarket]] = {}

    # Phase 1: list markets per venue, unchanged from before this task —
    # two `list_markets` calls (one per venue today) are not the
    # rate-limit/skew problem T35 exists for; the ~800 `get_book` calls
    # below are. A venue that cannot be listed is skipped, not fatal.
    for venue, adapter in adapters.items():
        try:
            markets = await adapter.list_markets(status="open")
        except VenueError:
            logger.warning(
                "scanner", extra={"event": "list_markets_failed", "venue": venue}
            )
            continue
        top_markets = sorted(markets, key=_volume, reverse=True)[
            : settings_obj.scan_top_n
        ]
        top_markets_by_venue[venue] = top_markets
        for market in top_markets:
            markets_by_key[(venue, market.market_id)] = market

    # Phase 2: fetch EVERY book this pass needs, from EVERY venue,
    # TOGETHER, under one shared `asyncio.Semaphore` — see the module
    # docstring's "BOOK FETCH IS CONCURRENT..." section for why a single
    # global bound (not one per venue, not unbounded) is what addresses
    # both the rate-limit risk and the cross-venue snapshot-skew risk at
    # once, and why `_fetch_book` catches `VenueError` itself rather than
    # letting `asyncio.gather` see it.
    fetch_specs: list[tuple[VenueId, str, str]] = _interleave_fetch_specs(
        top_markets_by_venue
    )
    semaphore = asyncio.Semaphore(settings_obj.scan_book_fetch_concurrency)
    fetch_started = time.monotonic()
    fetch_results = await asyncio.gather(
        *(
            _fetch_book(adapters[venue], venue, market_id, outcome, semaphore)
            for venue, market_id, outcome in fetch_specs
        )
    )
    fetch_elapsed_s = time.monotonic() - fetch_started

    books_by_key: dict[tuple[str, str, str], OrderBook] = {}
    for fetched in fetch_results:
        if fetched is None:
            continue
        fetched_venue, fetched_market_id, fetched_outcome, book = fetched
        books_by_key[
            (fetched_venue, fetched_market_id, normalize_outcome(fetched_outcome))
        ] = book

    # The cheapest operator-facing signal for "were this pass's books
    # close enough together in time for a cross-venue edge to mean
    # anything": the wall-clock length of the WHOLE concurrent fetch
    # phase above. Not a per-book-pair skew table (a real measurement,
    # but a schema/API change outside this task's file set) — this is a
    # health metric for the pass, not a scored input.
    logger.info(
        "scanner",
        extra={
            "event": "scan_book_fetch_complete",
            "books_requested": len(fetch_specs),
            "books_fetched": sum(1 for fetched in fetch_results if fetched is not None),
            "concurrency_bound": settings_obj.scan_book_fetch_concurrency,
            "fetch_elapsed_s": round(fetch_elapsed_s, 3),
        },
    )

    # Phase 3: build every `MarketSnapshot` now that every book this pass
    # will ever fetch has already landed in `books_by_key`. Iterates
    # venues/markets in the EXACT same order the old single-pass loop
    # did (`top_markets_by_venue` was populated in that same order in
    # Phase 1), so `snapshots`' order — and therefore which opportunities
    # a given fixture yields — is unchanged; only WHEN and in what order
    # the network calls that filled `books_by_key` completed is
    # different, and nothing downstream reads that.
    snapshots: list[MarketSnapshot] = [
        _snapshot_from_market(market, books_by_key, now)
        for top_markets in top_markets_by_venue.values()
        for market in top_markets
    ]

    # `cross_venue_arbitrage` needs BOTH legs' books to price either
    # direction, but a `MarketSnapshot` carries only one outcome's book —
    # feed every fetched book through its `observe_book()` injection
    # point so it sees full per-outcome coverage regardless of scan
    # order. A no-op for any strategy without that method.
    for strategy in strategies.values():
        observe_book = getattr(strategy, "observe_book", None)
        if callable(observe_book):
            for book in books_by_key.values():
                observe_book(book)

    ctx = ScoreContext(
        now=now, books=books_by_key, markets=markets_by_key, settings=settings_obj
    )
    links_by_id: dict[int, EventLink] = {
        link.id: link for link in links if link.id is not None
    }

    scored: list[ScoredIntent] = []
    for strategy_name, strategy in strategies.items():
        for snapshot in snapshots:
            try:
                result = strategy.on_market_data(snapshot)
            except Exception:  # noqa: BLE001 - one strategy must not stop the scan
                logger.exception(
                    "scanner",
                    extra={
                        "event": "strategy_error",
                        "strategy": strategy_name,
                        "venue": snapshot.venue,
                        "market_id": snapshot.market_id,
                    },
                )
                continue
            if result is None:
                continue
            intent = (
                result
                if isinstance(result, Intent)
                else result.to_intent(
                    venue=snapshot.venue, expected_resolution_ts=snapshot.end_date
                )
            )
            _stamp_link_status(intent, links_by_id)
            try:
                opportunity_score = score(intent, ctx)
            except UnscorableIntent as exc:
                logger.info(
                    "scanner",
                    extra={
                        "event": "unscorable_intent",
                        "strategy": strategy_name,
                        "reason": str(exc),
                    },
                )
                continue
            record = _persist(
                intent,
                opportunity_score,
                strategy_name,
                scan_id,
                settings_obj.trading_mode,
                session,
                scan_pass=ARBITRAGE_SCAN_PASS,
            )
            scored.append(
                ScoredIntent(
                    intent=intent,
                    score=opportunity_score,
                    strategy=strategy_name,
                    intent_record_id=record.id,
                )
            )

    await session.commit()
    scored.sort(key=lambda scored_intent: scored_intent.score.composite, reverse=True)
    return scored


#: PLAN.md D10(b)/(a): additive `resolution_risk` penalties
#: `near_resolution_pass` stacks on top of `app.services.scoring.score`'s
#: own formula, capped the same way that function caps its own result.
_DISPUTE_WINDOW_RISK_ADD = 0.3
_LIQUIDITY_COLLAPSE_RISK_ADD = 0.2
_NEAR_RESOLUTION_RISK_CAP = 1.0

#: PLAN.md D10(a): `liquidity_collapse` fires when the current spread
#: exceeds its trailing-24h median by more than this multiple.
_LIQUIDITY_COLLAPSE_RATIO = 3.0

#: Trailing window `_liquidity_collapse` reads `PriceHistory.spread`
#: over, in hours (PLAN.md D10(a): "vs 24h median").
_LIQUIDITY_HISTORY_WINDOW_HOURS = 24.0


def _in_dispute_window(market: VenueMarket, now: datetime) -> bool:
    """Return whether `market` is inside its dispute/settlement-timer window.

    See the module docstring's "THE THREE MARKET-LEVEL SIGNALS" section.
    Polymarket has no `expected_settle_time` (its adapter always sets it
    `None`), so its reference is `close_time`; Kalshi's is its own,
    later `expected_settle_time` (`expected_expiration_time`).

    Args:
        market: The market to check.
        now: Aware UTC "as of" time.

    Returns:
        bool: `False` if `market.status == "resolved"` (a resolved
            market is settled, not disputed); otherwise whether the
            venue-appropriate reference timestamp is at or before `now`.
    """
    if market.status == "resolved":
        return False
    reference = (
        market.expected_settle_time if market.venue == "kalshi" else market.close_time
    )
    if reference is None:
        reference = market.close_time
    return reference <= now


def _expected_settlement_ts(
    market: VenueMarket, now: datetime, settings_obj: Settings
) -> datetime:
    """Return this pass's best estimate of `market`'s real settlement time.

    Args:
        market: The market to estimate for.
        now: Aware UTC "as of" time.
        settings_obj: Supplies `settlement_delay_hours`.

    Returns:
        datetime: `market.expected_settle_time` when the venue states one
            AND it is still in the future (Kalshi, the common case);
            otherwise `now + settings_obj.settlement_delay_hours`. This
            covers THREE cases with one formula: Polymarket, whose
            adapter never populates `expected_settle_time` at all;
            Kalshi BEFORE its own expected settlement (the value is used
            as stated); and Kalshi's own expected settlement having
            ALREADY passed without a result (i.e. `_in_dispute_window`
            is already `True`) — using that stale, past timestamp
            verbatim here would make `hours_to_resolution` zero or
            negative and `SettlementEdgeStrategy.evaluate` would refuse
            to annualize at all, so a market already inside its dispute
            window instead gets a FRESH `settlement_delay_hours`
            estimate measured from `now`, same as a venue that never
            told us a settlement time to begin with.
    """
    if market.expected_settle_time is not None and market.expected_settle_time > now:
        return market.expected_settle_time
    return now + timedelta(hours=settings_obj.settlement_delay_hours)


async def _liquidity_collapse(
    session: AsyncSession,
    market: VenueMarket,
    spread_now: float | None,
    now: datetime,
) -> tuple[bool, bool]:
    """Return `(liquidity_collapse, unavailable)` for `market` (PLAN.md D10(a)).

    See the module docstring's "THE THREE MARKET-LEVEL SIGNALS" section
    for why `unavailable=True` must never be read as "measured fine".

    Args:
        session: Read `PriceHistory` rows from.
        market: The market to check (its `market_id`, venue-qualified
            the same way every other `PriceHistory` reader in this repo
            already treats that column — see
            `app.services.data_collector`).
        spread_now: The market's current `yes_ask - yes_bid`, or `None`
            if no book was found for it this pass.
        now: Aware UTC "as of" time; the trailing window is
            `[now - 24h, now]`.

    Returns:
        tuple[bool, bool]: `(liquidity_collapse, unavailable)`.
            `unavailable=True` (with `liquidity_collapse=False`, the
            conservative default — see GUARDRAILS.md §1.7) when
            `spread_now` is `None`, no `PriceHistory` rows exist in the
            window, or their median spread is not a usable positive
            value; otherwise `liquidity_collapse = spread_now / median >
            _LIQUIDITY_COLLAPSE_RATIO` and `unavailable=False`.
    """
    if spread_now is None or spread_now < 0.0:
        return False, True
    since = now - timedelta(hours=_LIQUIDITY_HISTORY_WINDOW_HOURS)
    rows = (
        await session.execute(
            select(PriceHistory.spread).where(
                PriceHistory.market_id == market.market_id,
                PriceHistory.timestamp >= since,
                PriceHistory.timestamp <= now,
            )
        )
    ).scalars().all()
    spreads = sorted(value for value in rows if value is not None)
    if not spreads:
        return False, True
    mid = len(spreads) // 2
    median = (
        spreads[mid]
        if len(spreads) % 2 == 1
        else (spreads[mid - 1] + spreads[mid]) / 2.0
    )
    if median <= 0.0:
        return False, True
    return (spread_now / median) > _LIQUIDITY_COLLAPSE_RATIO, False


def _apply_near_resolution_risk(
    base: OpportunityScore,
    *,
    annualized_return: float,
    in_dispute_window: bool,
    liquidity_collapse: bool,
) -> OpportunityScore:
    """Overlay the near-resolution bucket's own risk/annualization onto `base`.

    See the module docstring's "WHY THIS PASS BUILDS ITS OWN
    `OpportunityScore`..." section for the full rationale.

    `annualized_return` is NOT recomputed here from `base.net_edge` —
    `score()`'s generic formula (`net_edge / floor_hours * HOURS_PER_YEAR`)
    never divides by the capital actually deployed, which is an
    adequate approximation for `binary_complement_arbitrage`/
    `cross_venue_arbitrage` (their combined ask sums are close to $1.00,
    so a per-contract dollar edge and a fractional return are close
    together) but NOT here, where a near-certain ask can sit anywhere in
    `(0, 1)` and the difference is exactly the point (a $0.0095 edge on
    a $0.98 ask is a very different return than the same $0.0095 on a
    $0.10 ask). `SettlementEdgeStrategy.evaluate` already computed the
    correct, capital-normalized figure — `(1 - ask - fee - gas) / ask`,
    floored at `max(hours_to_resolution, settlement_delay_hours)`, per
    PLAN.md D10(c) verbatim — and published it as
    `intent.metadata["annualized_return"]`; this function's caller reads
    that back and passes it straight through here instead of
    re-deriving a different, wrong number from `base.net_edge`.

    Args:
        base: `score(intent, ctx, allow_past_close=True)`'s result —
            every field except `annualized_return`/`resolution_risk`/
            `composite` is carried over unchanged.
        annualized_return: `intent.metadata["annualized_return"]`, the
            strategy-published, already-correctly-floored figure.
        in_dispute_window: Adds `_DISPUTE_WINDOW_RISK_ADD` when `True`.
        liquidity_collapse: Adds `_LIQUIDITY_COLLAPSE_RISK_ADD` when
            `True`.

    Returns:
        OpportunityScore: `base` with `annualized_return` replaced by
            the strategy-published figure, `resolution_risk` bumped and
            re-capped, and `composite` recomputed from those two plus
            the unchanged `fill_confidence`.
    """
    resolution_risk = base.resolution_risk
    if in_dispute_window:
        resolution_risk += _DISPUTE_WINDOW_RISK_ADD
    if liquidity_collapse:
        resolution_risk += _LIQUIDITY_COLLAPSE_RISK_ADD
    resolution_risk = min(_NEAR_RESOLUTION_RISK_CAP, resolution_risk)
    composite = annualized_return * base.fill_confidence * (1.0 - resolution_risk)
    return replace(
        base,
        annualized_return=annualized_return,
        resolution_risk=resolution_risk,
        composite=composite,
    )


async def near_resolution_pass(
    adapters: Mapping[VenueId, MarketDataAdapter],
    session: AsyncSession,
    *,
    strategy_config: Mapping[str, Any] | None = None,
    settings_obj: Settings = default_settings,
) -> list[ScoredIntent]:
    """Run `SettlementEdgeStrategy` over every near-resolution market. Never routes.

    See the module docstring for why this is a SEPARATE pass from
    `scan()` (scope: `settlement_edge` only), how it resolves the
    `close_time`-passed tension with `app.services.scoring.score`, and
    what the three market-level risk signals mean.

    Args:
        adapters: One READ-ONLY adapter per venue (same contract as
            `scan()` — `app.venues.base.MarketDataAdapter`, from
            `app.venues.registry.get_read_adapter`, NEVER an
            order-placing adapter).
        session: Persists every scored `IntentRecord`; this function
            commits once at the end of the pass, same as `scan()`.
        strategy_config: Optional override merged into
            `SettlementEdgeStrategy`'s `DEFAULT_CONFIG` (e.g. a test
            setting `allow_dispute_window=True`). `None` uses the
            strategy's shipped defaults.
        settings_obj: `Settings` to select/score/risk-adjust against.
            Defaults to the process-wide singleton; a test passes an
            explicit instance.

    Returns:
        list[ScoredIntent]: Every settlement-edge intent this pass
            scored, sorted by `score.composite` descending. Every
            `intent.metadata["bucket"] == "near_resolution"` (stamped by
            the strategy itself — see `SettlementEdgeStrategy.evaluate`)
            and additionally carries `"liquidity_collapse"` and
            `"liquidity_collapse_unavailable"` (stamped here).
    """
    now = utcnow()
    scan_id = str(uuid.uuid4())
    strategy = SettlementEdgeStrategy(dict(strategy_config) if strategy_config else None)

    scored: list[ScoredIntent] = []
    for venue, adapter in adapters.items():
        try:
            # `status=None`, not `"open"`: see the module docstring's
            # "Market SELECTION" paragraph for why `scan()`'s filter
            # would silently drop a Kalshi market this pass needs.
            markets = await adapter.list_markets(status=None)
        except VenueError:
            logger.warning(
                "scanner",
                extra={"event": "near_resolution_list_markets_failed", "venue": venue},
            )
            continue

        for market in markets:
            if market.status == "resolved":
                continue
            hours_to_close = (market.close_time - now).total_seconds() / 3600.0
            if hours_to_close > settings_obj.near_resolution_hours:
                continue

            books_by_key: dict[tuple[str, str, str], OrderBook] = {}
            for outcome in market.outcomes:
                try:
                    book = await adapter.get_book(market.market_id, outcome)
                except VenueError:
                    logger.debug(
                        "scanner",
                        extra={
                            "event": "near_resolution_get_book_failed",
                            "venue": venue,
                            "market_id": market.market_id,
                            "outcome": outcome,
                        },
                    )
                    continue
                books_by_key[
                    (venue, market.market_id, normalize_outcome(outcome))
                ] = book

            snapshot = _snapshot_from_market(market, books_by_key, now)
            in_dispute_window = _in_dispute_window(market, now)
            liquidity_collapse, liquidity_unavailable = await _liquidity_collapse(
                session, market, snapshot.spread, now
            )
            expected_resolution_ts = _expected_settlement_ts(market, now, settings_obj)
            # Every outcome's REAL book this market fetched, keyed by
            # outcome only (every key in `books_by_key` shares this
            # iteration's `venue`/`market.market_id`) -- so
            # `SettlementEdgeStrategy.evaluate` can price whichever side
            # `outcome_determined` actually names, never the single,
            # always-YES-for-a-binary-market `snapshot.book`
            # `_snapshot_from_market` builds (see that function's and
            # `_priced_fills`'s docstrings; T21c).
            outcome_books = {key[2]: book for key, book in books_by_key.items()}

            try:
                intent = strategy.evaluate(
                    snapshot,
                    in_dispute_window=in_dispute_window,
                    expected_resolution_ts=expected_resolution_ts,
                    outcome_books=outcome_books,
                )
            except Exception:  # noqa: BLE001 - one market must not stop the pass
                logger.exception(
                    "scanner",
                    extra={
                        "event": "near_resolution_strategy_error",
                        "venue": venue,
                        "market_id": market.market_id,
                    },
                )
                continue
            if intent is None:
                continue

            intent.metadata["liquidity_collapse"] = liquidity_collapse
            intent.metadata["liquidity_collapse_unavailable"] = liquidity_unavailable

            ctx = ScoreContext(
                now=now,
                books=books_by_key,
                markets={(venue, market.market_id): market},
                settings=settings_obj,
            )
            try:
                base_score = score(intent, ctx, allow_past_close=True)
            except UnscorableIntent as exc:
                logger.info(
                    "scanner",
                    extra={
                        "event": "near_resolution_unscorable_intent",
                        "strategy": strategy.name,
                        "reason": str(exc),
                    },
                )
                continue

            adjusted_score = _apply_near_resolution_risk(
                base_score,
                annualized_return=float(intent.metadata["annualized_return"]),
                in_dispute_window=in_dispute_window,
                liquidity_collapse=liquidity_collapse,
            )

            record = _persist(
                intent,
                adjusted_score,
                strategy.name,
                scan_id,
                settings_obj.trading_mode,
                session,
                scan_pass=NEAR_RESOLUTION_SCAN_PASS,
            )
            scored.append(
                ScoredIntent(
                    intent=intent,
                    score=adjusted_score,
                    strategy=strategy.name,
                    intent_record_id=record.id,
                )
            )

    await session.commit()
    scored.sort(key=lambda scored_intent: scored_intent.score.composite, reverse=True)
    return scored
