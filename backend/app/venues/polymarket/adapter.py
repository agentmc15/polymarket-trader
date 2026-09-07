"""Polymarket `VenueAdapter` read path (PLAN.md D3, T11).

`PolymarketAdapter` talks to Polymarket over two `httpx.AsyncClient`
instances — one for the Gamma API (`settings.gamma_api_url`, market
metadata) and one for the CLOB API (`settings.clob_api_url`, order books
and, via the synchronous `py_clob_client`, credentialed account data).
Both accept a single injected `transport` (GUARDRAILS.md §4;
`httpx.MockTransport` in tests — GUARDRAILS.md §1.4 forbids real network
access here).

Units (GUARDRAILS.md §4): prices are probabilities in `[0.0, 1.0]`; sizes
are contracts (each pays $1.00 at resolution). The CLOB sends price/size
as decimal STRINGS (PLAN.md §3) — converted to `float` at parse time in
this module and nowhere else.

GUARDRAILS.md §1.1: this module (and `app/venues/polymarket/__init__.py`)
must NEVER invoke the `ClobClientWrapper` methods that create, submit, or
withdraw a real CLOB order — those live ONLY in `app/venues/polymarket/
live.py`'s live-adapter subclass, the sole class allowed to place,
modify, or cancel a real order.

Fee-schedule source precedence (PLAN.md §3, brief acceptance): a
market's `FeeSchedule` defaults to `category_fee_schedule(category)`
(source `"category_table"`, or `"settings_override"` when
`settings.polymarket_taker_fee_overrides` supplied the rate — Phase-1
remediation FIX 3, `app/venues/fees.py`); if the CLOB market payload for
that market carries
`taker_base_fee` (even `0`, i.e. an explicit fee waiver — checked via
`is not None`, not truthiness), that rate is authoritative and overrides
the category default (source `"clob_market"`). CLOB fee rates are
basis-point integers (verified by inspecting the installed
`py_clob_client` package: `ClobClient.get_fee_rate_bps`/the `/fee-rate`
endpoint's `base_fee` key are bps, not a `[0,1]` rate — GUARDRAILS.md
§1.4 forbids re-fetching vendor docs but does not forbid reading an
already-installed, already-vendored local package), so they are divided
by `10_000` here to get the dimensionless rate `FeeSchedule` expects.

`get_market`/`list_markets` enrich Gamma's market metadata (question,
outcomes, description, category, ...) with the CLOB market payload's
`tick_size`/`min_order_size` (-> `VenueMarket.tick_size`/`min_size`,
per an explicit orchestrator ruling: `OrderBook` has no such fields —
those live on `VenueMarket`, T07's fill engine reads them from there) and
`taker_base_fee`/`maker_base_fee`. `list_markets` enriches from the CLOB
market LIST endpoint (one call, keyed by `condition_id`) rather than one
CLOB call per market, to avoid an N+1 fan-out; `get_market` enriches from
the CLOB single-market endpoint directly. Pagination of the CLOB market
list is NOT implemented in this kit (single page only) — acceptable at
fixture/kit scale; flagged for whoever wires this against a large, real
market catalog.

`get_book` IS FOUR HTTP REQUESTS, NOT ONE — WHICH IS WHY THE MARKET
LOOKUP IS MEMOIZED (T38 F6). The CLOB book endpoint is keyed by
`token_id`, so `get_book` must first resolve `(market_id, outcome) ->
token_id` through `get_market()`, and `get_market()` is itself three
requests: `GET gamma/markets?condition_ids=...`, `GET gamma/events` (the
FULL events listing, for `event_id`), and `GET clob/markets/{condition_
id}`. Measured with a counting `httpx.MockTransport`: one `get_book`
issued four requests, four `get_book` calls issued sixteen. At
`scan_top_n=200` that is ~1600 Polymarket requests a scan pass for 400
books, 400 of them full events listings — where Kalshi's `get_book` is
exactly one request, which is also why Polymarket's leg of a pass ran
several times longer than Kalshi's. `_AsyncTtlMemo` (below) memoizes the
events listing under one key and the per-market metadata under
`market_id`, single-flight so a market's two concurrent outcome fetches
share one lookup, for `settings.polymarket_market_cache_ttl_s`. The BOOK
is never memoized. `get_market()` itself is deliberately left unmemoized
— it is a public read whose callers are entitled to a fresh answer.

The CLOB book endpoint's OWN `tick_size`/`min_order_size` (which may
legitimately differ slightly from the market payload's, or simply
duplicate it) are recorded on `OrderBook.metadata`, NOT on `OrderBook`
itself — `OrderBook` is a frozen, normalized type with no such fields,
by design (T04); adding them there would duplicate `VenueMarket`'s
authoritative copy.

Credentialed methods (`get_balance`, `get_positions`, `get_open_orders`,
`get_fills`) require `settings.polymarket_private_key`; when it is empty
they raise `VenueAuthError` (not `ValueError`) BEFORE touching the
network or `py_clob_client` at all. When credentials ARE present, the
underlying `py_clob_client` calls are genuinely synchronous and are run
via `asyncio.to_thread` so they never block the event loop (PLAN.md §3).
NOTE ON SCOPE: `ClobClientWrapper` (`app/services/polymarket/client.py`)
has convenience methods for orders/trades but none for balance or
positions; `py_clob_client` itself has no dedicated positions endpoint
either (verified against the installed package's `ClobClient` method
list). Neither PLAN.md §3 nor the installed client pins these two
payload shapes the way it pins the book/fee/order shapes, so
`get_balance`/`get_positions` are necessarily best-effort here — read the
docstrings on `_positions_from_trades`/`get_balance` for exactly what is
(and is not) modeled, and treat them as a documented gap for whoever
wires real Polymarket credentials for the first time to verify against a
live response before trusting them for sizing.
"""
import asyncio
import json
import logging
import math
import threading
import time
from collections.abc import Callable, Coroutine, Mapping
from datetime import UTC, datetime
from typing import Any, Generic, Literal, TypeVar, cast, overload

import httpx
from py_clob_client.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    OpenOrderParams,
    TradeParams,
)

from app.config import settings
from app.services.polymarket.client import ClobClientWrapper
from app.utils.time import ensure_aware, utcnow
from app.venues.base import (
    BaseAdapter,
    FeeModel,
    VenueAuthError,
    VenueError,
    VenuePayloadError,
)
from app.venues.fees import PolymarketFeeModel, category_fee_schedule
from app.venues.types import (
    Balance,
    BookLevel,
    FeeSchedule,
    Fill,
    Liquidity,
    MarketStatus,
    OrderAck,
    OrderBook,
    Position,
    VenueId,
    VenueMarket,
)

logger = logging.getLogger(__name__)

#: `OrderAck.status`'s literal type, named here so parsers can `cast` a
#: validated `str` back to it without reaching for `Any`.
_OrderAckStatus = Literal["open", "filled", "partially_filled", "cancelled", "rejected"]
#: Items per Gamma `/markets` page. Gamma caps a page at 100 regardless of
#: a larger `limit`, and DEFAULTS TO 20 when none is sent — which is what
#: `list_markets` was getting.
#: `FeeSchedule.source` for a rate the venue published on the market
#: itself, which outranks the hand-maintained category table.
_FEE_SOURCE_VENUE = "venue_schedule"

_GAMMA_PAGE_LIMIT = 100

#: Pages `_fetch_gamma_markets` will walk. Gamma REFUSES an offset past
#: 2000 with a 422 (measured: offset 2000 -> 200, offset 2050 -> 422), so
#: 21 pages of 100 walks right up to that ceiling and no further. The
#: venue's own limit is what bounds this, not a number we chose.
_MAX_GAMMA_PAGES = 21

_ORDER_ACK_STATUSES: frozenset[str] = frozenset(
    {"open", "filled", "partially_filled", "cancelled", "rejected"}
)

_T = TypeVar("_T")

#: Hard cap on how many entries an `_AsyncTtlMemo` retains. It matters
#: because in `"paper"` mode `app.venues.registry.get_read_adapter`
#: returns the PROCESS-WIDE `PaperVenueAdapter` singleton, which wraps
#: one long-lived `PolymarketAdapter` — so a memo on that instance is
#: process-lifetime, not pass-lifetime, and an unbounded one would grow
#: a `market_id`-keyed entry for every market the process ever saw.
_MEMO_MAX_ENTRIES = 4096


class _AsyncTtlMemo(Generic[_T]):
    """A short-lived, single-flight memo for one expensive async lookup.

    WHY THIS EXISTS (T38 F6). `PolymarketAdapter.get_book` resolves an
    outcome name to its CLOB `token_id` by calling `get_market()` first,
    and `get_market()` is THREE HTTP requests: `GET gamma/markets?
    condition_ids=...`, `GET gamma/events` (the FULL events listing, so
    the market can be tagged with its `event_id`), and `GET clob/markets/
    {condition_id}`. So one `get_book` is four requests, not one — and a
    `scan()` pass over `scan_top_n=200` markets x 2 outcomes issues
    ~1600 Polymarket requests, 400 of them full `/events` listings, for
    400 books. (Kalshi's `get_book` is exactly one request, which is
    also why Polymarket's leg of a pass runs several times longer than
    Kalshi's — the "one venue 4x slower" shape that widens pair skew.)

    `settings.scan_book_fetch_concurrency`'s own comment justifies its
    bound as a rate-limit budget. That bound still holds as a RATE — the
    four requests inside one `get_book` are sequential, so at most
    `scan_book_fetch_concurrency` are ever in flight — but the pass's
    request VOLUME was 4x what "800 `get_book` calls" implies, and the
    repeat work was pure waste: the same events listing 400 times, and
    the same market's metadata once per outcome.

    SINGLE-FLIGHT, NOT JUST A CACHE. A plain value cache barely helps
    here, because the calls that duplicate each other run CONCURRENTLY:
    a market's YES and NO specs are adjacent in `scan()`'s interleaved
    fetch order, so both miss an empty cache and both fetch. The
    per-key lock below is what turns "up to `bound` duplicate lookups"
    into exactly one. It is double-checked: a waiter re-reads the cache
    after acquiring, and if the leader FAILED (nothing cached) it simply
    does its own fetch rather than inheriting the leader's exception.

    STALENESS IS BOUNDED BY `ttl_s`, DELIBERATELY SHORT. What is
    memoized is market METADATA (`outcome_ids`, `tick_size`, `status`)
    and event grouping — data that turns over on a scale of hours. The
    TTL exists so that a long-lived adapter cannot serve a market's
    metadata indefinitely, not because the data is volatile. Order books
    are NEVER memoized: `GET /book` is issued on every single
    `get_book` call, which is the whole point of the pass.

    BOTH TABLES ARE CAPPED, AND THE LOCK TABLE IS CAPPED WHERE LOCKS ARE
    BORN (T43 F3). `_MEMO_MAX_ENTRIES` exists because a paper-mode memo
    is process-lifetime, and until T43 it bounded `_values` only: the
    lock sweep lived below `_store`'s under-cap early return, so it ran
    only when a SUCCESSFUL store had pushed the value table over the cap.
    A factory that always RAISES never stores anything, so `_values`
    never grew, so the sweep never ran — while `_lock_for` kept minting a
    lock per key. 10,000 failing lookups left `_values` empty and
    `_locks` at 10,000. The sweep is therefore in `_lock_for` now, on the
    only path that can grow that table, and `_store` bounds values only.

    THREAD SAFETY (T43 F4). Both tables are mutated under `_guard`, a
    plain `threading.Lock`. `_lock_for` is a check-then-mutate on state
    reachable from a PROCESS-WIDE singleton, so two OS threads each
    inside their own `asyncio.run` could interleave between the loop
    check and the `_locks.get` and hand a waiter a lock another loop had
    already contended (`RuntimeError: ... is bound to a different event
    loop`), or tear `_store`'s comprehensions apart mid-rebuild
    (`RuntimeError: dictionary changed size during iteration`). Both
    reproduce under `sys.setswitchinterval(1e-7)`, neither at the default
    5 ms — and the shipped Celery prefork pool gives one `asyncio.run`
    per PROCESS, so today there is no second loop over this object. It
    goes live the day anyone runs `--pool=threads` or `gevent`, or drives
    the paper singleton from a worker thread, which is a one-word config
    change nobody would connect to this file. The critical sections are a
    handful of dict operations with no `await` in them, so serializing
    them costs a lock acquisition against work whose next step is an HTTP
    request; documenting the hazard instead would have been the more
    expensive option. `_cached` stays outside the guard deliberately: it
    only reads, and both a `dict.get` and the rebinding it may race are
    individually atomic under the GIL, so it sees either table whole.

    Attributes:
        ttl_s: How long a cached value is served for. `<= 0` disables
            the memo entirely — every call goes to `factory`.
    """

    def __init__(self, ttl_s: float) -> None:
        """Build an empty memo with the given time-to-live in seconds."""
        self.ttl_s = ttl_s
        self._values: dict[str, tuple[float, _T]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        #: Serializes every MUTATION of the two tables above. See the
        #: class docstring's THREAD SAFETY note for why a process-wide
        #: singleton needs one and why it is cheap here.
        self._guard = threading.Lock()

    def _cached(self, key: str) -> tuple[_T] | None:
        """Return `(value,)` if `key` is cached and unexpired, else `None`.

        A one-tuple rather than the bare value so that a legitimately
        falsy cached value (an empty mapping — Gamma `/events` returning
        no groupings is normal) is still a HIT.
        """
        entry = self._values.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if time.monotonic() - stored_at >= self.ttl_s:
            return None
        return (value,)

    def _lock_for(self, key: str) -> asyncio.Lock:
        """Return this key's lock, rebuilding the table if the loop changed.

        WHY, PRECISELY (verified on CPython 3.12.7, not assumed).
        `asyncio.Lock` binds itself to a loop only on the CONTENDED
        path — an uncontended `acquire()` never calls `_get_loop()` at
        all, so a lock used by one caller at a time really is portable
        across loops. What raises `RuntimeError: ... is bound to a
        different event loop` is a lock that was CONTENDED under one
        loop and is contended again under another.

        That combination is the ordinary case here, not an exotic one.
        `app.tasks.scanner` runs each beat under its own `asyncio.run`,
        and in `"paper"` mode the adapter is a process-wide singleton
        that outlives every one of them. Within a single beat,
        `scan()`'s interleaved fetch order puts a market's YES and NO
        books in the same admission wave, so they contend that market's
        lock every time. And with the default TTL at half
        `scan_interval_s`, every memo entry has expired by the next
        beat, so the next beat contends the SAME lock again — the exact
        two-loops-both-contended shape. A loop change therefore discards
        the whole lock table. Cached VALUES are plain data and survive
        it.

        THIS IS ALSO WHERE THE LOCK TABLE IS CAPPED (T43 F3). Minting a
        lock is the only thing that grows it, and it happens on every
        MISS — including the misses of a factory that raises, which store
        nothing and so used to slip past a sweep that only ran when the
        value table went over cap. Sweeping here bounds the table on the
        path that fills it. Only UNHELD locks are dropped, and dropping
        one is safe: the worst case is that a newcomer builds a fresh one
        and performs one redundant fetch. There is no window in which a
        caller loses the lock it was just handed — `get` acquires it with
        no `await` in between — so single-flight is unaffected.

        The whole body runs under `_guard`; see the class docstring.
        """
        loop = asyncio.get_running_loop()
        with self._guard:
            if self._lock_loop is not loop:
                self._lock_loop = loop
                self._locks = {}
            lock = self._locks.get(key)
            if lock is None:
                if len(self._locks) >= _MEMO_MAX_ENTRIES:
                    self._locks = {
                        lock_key: held
                        for lock_key, held in self._locks.items()
                        if held.locked()
                    }
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    def _store(self, key: str, value: _T) -> None:
        """Cache `value` under `key`, evicting expired entries when over cap.

        Values only. The lock table is bounded in `_lock_for`, where
        locks are created (T43 F3) — a sweep here could not see the keys
        a FAILING factory leaves behind, because a failure never reaches
        this method at all. Runs under `_guard`; see the class docstring.
        """
        with self._guard:
            self._values[key] = (time.monotonic(), value)
            if len(self._values) <= _MEMO_MAX_ENTRIES:
                return
            now = time.monotonic()
            self._values = {
                k: entry
                for k, entry in self._values.items()
                if now - entry[0] < self.ttl_s
            }
            if len(self._values) > _MEMO_MAX_ENTRIES:
                self._values.clear()

    async def get(
        self, key: str, factory: Callable[[], Coroutine[Any, Any, _T]]
    ) -> _T:
        """Return the memoized value for `key`, calling `factory` at most once.

        Args:
            key: Cache key.
            factory: Zero-argument coroutine function producing the
                value. Called only on a miss, and only by the single
                caller that wins this key's lock.

        Returns:
            _T: The cached or freshly produced value.

        Raises:
            Exception: Whatever `factory` raises, unchanged and
                UNCACHED — a failed lookup is retried by the next
                caller rather than remembered.
        """
        if self.ttl_s <= 0.0:
            return await factory()
        hit = self._cached(key)
        if hit is not None:
            return hit[0]
        async with self._lock_for(key):
            hit = self._cached(key)
            if hit is not None:
                return hit[0]
            value = await factory()
            self._store(key, value)
            return value


class PolymarketAdapter(BaseAdapter):
    """Read-path `VenueAdapter` for Polymarket (PLAN.md D3).

    `PolymarketLiveAdapter` (`app/venues/polymarket/live.py`) subclasses
    this to add real order placement and cancellation — the ONLY methods
    in the package that do (GUARDRAILS.md §1.1).
    """

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        market_cache_ttl_s: float | None = None,
    ) -> None:
        """Build the two API clients this adapter needs.

        Args:
            transport: Optional injected `httpx` transport. Tests pass an
                `httpx.MockTransport` here (GUARDRAILS.md §1.4: no network
                access in tests); production code leaves this `None` so
                `httpx.AsyncClient` uses its real transport.
            market_cache_ttl_s: Seconds a market-metadata / event-grouping
                lookup is memoized for (see `_AsyncTtlMemo` for the
                request-amplification this exists to remove). `None`
                reads `settings.polymarket_market_cache_ttl_s`; `0.0`
                disables memoization entirely.
        """
        self.venue: VenueId = "polymarket"
        # Kept so the clients can be REBUILT when the event loop changes;
        # see `_client_for`. Tests inject a MockTransport here and it must
        # survive a rebuild, or a rebuilt client would reach the network.
        self._transport = transport
        self._client_loop: asyncio.AbstractEventLoop | None = None
        self._gamma = httpx.AsyncClient(
            base_url=settings.gamma_api_url, transport=transport, timeout=30.0
        )
        self._clob = httpx.AsyncClient(
            base_url=settings.clob_api_url, transport=transport, timeout=30.0
        )
        self._clob_wrapper: ClobClientWrapper | None = None
        ttl_s = (
            settings.polymarket_market_cache_ttl_s
            if market_cache_ttl_s is None
            else market_cache_ttl_s
        )
        #: Gamma `/events` is a FULL listing and is identical for every
        #: caller, so one entry under a constant key serves the whole
        #: TTL — this is the single biggest saving (400 listings a pass
        #: became 1).
        self._events_memo: _AsyncTtlMemo[dict[str, str]] = _AsyncTtlMemo(ttl_s)
        #: Keyed by `market_id`. Serves `get_book`'s outcome -> token_id
        #: resolution ONLY: a market's two outcomes cost one lookup
        #: between them instead of one each, and `get_market()` never
        #: reads this memo, so its two per-market legs (`GET gamma/
        #: markets?condition_ids=...` and `GET clob/markets/{id}`) are
        #: re-issued on every call. Its callers — the matcher, the links
        #: API — are entitled to a fresh answer and nothing about F6 is
        #: their fault.
        #:
        #: `get_market()` IS PARTIALLY MEMOIZED, and an earlier version
        #: of this comment claimed otherwise (T43). Its third leg, `GET
        #: gamma/events`, goes through `_event_id_by_market_id` and so
        #: through the SHARED `_events_memo` above — measured, the first
        #: call issues `/markets`, `/events` and `/markets/{id}` and the
        #: second issues only `/markets` and `/markets/{id}`. Exactly one
        #: field is affected, `VenueMarket.event_id`, which may be up to
        #: `ttl_s` stale; event grouping turns over on a scale of hours
        #: and nothing in `app/` reads that field, so the staleness is
        #: harmless — but "stays UNMEMOIZED" was a guarantee this code
        #: does not make.
        self._book_market_memo: _AsyncTtlMemo[VenueMarket] = _AsyncTtlMemo(ttl_s)


    def _rebind_clients_if_loop_changed(self) -> None:
        """Rebuild both clients when the running event loop has changed.

        In paper mode `make_paper_adapter` hands back a PROCESS-WIDE
        singleton on purpose -- its in-memory orders are the only record
        there is -- so this adapter outlives any one event loop. Each
        Celery beat tick runs `asyncio.run(...)`, which creates a loop
        and closes it on the way out, and an `httpx.AsyncClient` holds
        connections bound to the loop that opened them.

        The result, observed against the live API: tick 1 of a beat
        succeeds, and every tick afterwards dies inside the transport
        with `RuntimeError: Event loop is closed`. Retrying never helps,
        because the dead pool is reused forever. No test could see it --
        the suite runs one loop per process.

        Rebuilding is correct rather than defensive: connections owned by
        a closed loop are unusable, and the injected transport is
        preserved so a rebuilt client in a test still cannot reach the
        network.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._client_loop is loop and not self._gamma.is_closed:
            return
        self._gamma = httpx.AsyncClient(
            base_url=settings.gamma_api_url, transport=self._transport, timeout=30.0
        )
        self._clob = httpx.AsyncClient(
            base_url=settings.clob_api_url, transport=self._transport, timeout=30.0
        )
        self._client_loop = loop

    @property
    def _gamma_client(self) -> httpx.AsyncClient:
        """The Gamma client, rebound to the running loop if needed."""
        self._rebind_clients_if_loop_changed()
        return self._gamma

    @property
    def _clob_client(self) -> httpx.AsyncClient:
        """The CLOB client, rebound to the running loop if needed."""
        self._rebind_clients_if_loop_changed()
        return self._clob

    async def aclose(self) -> None:
        """Close both underlying `httpx.AsyncClient` instances."""
        await self._gamma.aclose()
        await self._clob.aclose()

    # -- Market metadata -----------------------------------------------

    async def list_markets(
        self,
        status: MarketStatus | None = None,
        updated_since: datetime | None = None,
    ) -> list[VenueMarket]:
        """List Polymarket markets, optionally filtered.

        Args:
            status: Only return markets in this status, if given.
            updated_since: Only return markets updated at/after this
                aware UTC timestamp, if given. Markets whose Gamma
                payload carries no parseable `updatedAt` are always kept
                (an unknown update time is not evidence of staleness).

        Returns:
            list[VenueMarket]: Matching markets, enriched with CLOB
                `tick_size`/`min_size`/fee-override data where available.
        """
        if updated_since is not None:
            ensure_aware(updated_since)
        gamma_items = await self._fetch_gamma_markets()
        event_id_by_market = await self._event_id_by_market_id()
        clob_by_condition_id = await self._fetch_clob_markets_by_condition_id()
        # One unbuildable market must not blank the whole venue. This is
        # the T38 property applied to the LISTING: `_build_market` raises
        # `VenuePayloadError` on, for example, a market with no `endDate`,
        # and Gamma really does serve those — invisible until pagination
        # widened the sample from 20 markets to ~2100, at which point a
        # single one of them aborted every scan.
        #
        # A TARGETED `get_market()` still raises: asking for one market and
        # getting silence would be the quiet failure. Here the caller asked
        # "what is listed", and the honest answer is everything that parsed,
        # with a count of what did not.
        markets = []
        skipped: dict[str, int] = {}
        for item in gamma_items:
            try:
                markets.append(
                    self._build_market(
                        item,
                        self._event_id_from_item(item)
                        or event_id_by_market.get(_market_id(item) or ""),
                        clob_by_condition_id.get(_market_id(item) or ""),
                    )
                )
            except VenueError as exc:
                reason = str(exc).split(":")[0][:60]
                skipped[reason] = skipped.get(reason, 0) + 1
        if skipped:
            logger.warning(
                "polymarket",
                extra={
                    "event": "gamma_markets_skipped",
                    "listed": len(gamma_items),
                    "built": len(markets),
                    "skipped": sum(skipped.values()),
                    "reasons": skipped,
                },
            )
        if status is not None:
            markets = [m for m in markets if m.status == status]
        if updated_since is not None:
            markets = [
                m
                for m in markets
                if (updated_at := _gamma_updated_at(m.raw)) is None
                or updated_at >= updated_since
            ]
        return markets

    async def get_market(self, market_id: str) -> VenueMarket:
        """Fetch one Polymarket market by its condition id.

        Args:
            market_id: Polymarket `condition_id` (or Gamma `id`).

        Returns:
            VenueMarket: The normalized market.

        Raises:
            VenuePayloadError: If Gamma has no market matching `market_id`.
        """
        gamma_items = await self._fetch_gamma_markets(
            params={"condition_ids": market_id}
        )
        match = next(
            (item for item in gamma_items if _market_id(item) == market_id), None
        )
        if match is None:
            raise VenuePayloadError(
                f"no Gamma market found for market_id={market_id!r}",
                raw=gamma_items,
            )
        event_id_by_market = await self._event_id_by_market_id()
        clob_item = await self._fetch_clob_market(market_id)
        return self._build_market(
            match,
            self._event_id_from_item(match) or event_id_by_market.get(market_id),
            clob_item,
        )

    def _build_market(
        self,
        gamma_item: dict[str, Any],
        event_id: str | None,
        clob_item: dict[str, Any] | None,
    ) -> VenueMarket:
        """Build a `VenueMarket` from a Gamma item plus optional CLOB enrichment.

        Raises:
            VenuePayloadError: If the payload carries no id, no
                `outcomes`, no parseable `endDate`, a `resolved`/`closed`
                flag that is not a boolean, a CLOB fee that is not in
                basis points, or values `VenueMarket` itself rejects.
        """
        market_id = _market_id(gamma_item)
        if market_id is None:
            raise VenuePayloadError(
                "gamma market payload missing id/conditionId", raw=gamma_item
            )
        # `str(gamma_item.get("question", ""))` would turn an explicit
        # `"question": null` into the literal string "None" — a market
        # displayed and scored under a title it does not have (T44).
        question = str(gamma_item.get("question") or "")
        if "outcomes" not in gamma_item:
            raise VenuePayloadError(
                f"gamma market {market_id} has no 'outcomes' — a market whose "
                "outcomes are unknown cannot be quoted, matched or traded, and "
                "must not be listed as if it could be",
                raw=gamma_item,
            )
        outcomes = tuple(str(o) for o in _parse_list_field(gamma_item["outcomes"]))
        if not outcomes:
            raise VenuePayloadError(
                f"gamma market {market_id} listed an EMPTY 'outcomes'", raw=gamma_item
            )
        token_ids_field = gamma_item.get("clobTokenIds", gamma_item.get("token_ids", []))
        token_ids = [str(t) for t in _parse_list_field(token_ids_field)]
        outcome_ids = dict(zip(outcomes, token_ids, strict=False))
        rules_text = str(gamma_item.get("description") or "")
        resolution_source_raw = gamma_item.get("resolutionSource")
        resolution_source = (
            str(resolution_source_raw) if resolution_source_raw else None
        )
        end_date = gamma_item.get("endDate")
        if not isinstance(end_date, str):
            raise VenuePayloadError("gamma market missing endDate", raw=gamma_item)
        close_time = _parse_iso8601(end_date)
        resolved = _to_bool(gamma_item.get("resolved"), field="resolved")
        closed = _to_bool(gamma_item.get("closed"), field="closed")
        status: MarketStatus = (
            "resolved" if resolved else ("closed" if closed else "open")
        )
        result = (
            _infer_result(outcomes, gamma_item.get("outcomePrices"))
            if status == "resolved"
            else None
        )
        category = gamma_item.get("category")
        category_str = str(category) if category is not None else None
        # The venue's OWN published rate first; the hand-maintained
        # category table is the fallback for markets that do not carry
        # one this model can honor.
        fee = _published_fee_schedule(gamma_item) or category_fee_schedule(
            category_str
        )
        tick_size = 0.01
        min_size = 0.0
        if clob_item is not None:
            tick_size = _clob_size_field(
                clob_item,
                ("minimum_tick_size", "tick_size"),
                market_id=market_id,
                label="tick size",
            )
            min_size = _clob_size_field(
                clob_item,
                ("minimum_order_size", "min_order_size"),
                market_id=market_id,
                label="minimum order size",
            )
            # CLOB's `taker_base_fee` is a legacy field reading 0 on
            # every live market while the venue charges 3-7% through
            # `feeSchedule`, so it must never override a rate the venue
            # published.
            taker_bps = clob_item.get("taker_base_fee")
            if taker_bps is not None and fee.source != _FEE_SOURCE_VENUE:
                maker_bps = clob_item.get("maker_base_fee")
                fee = FeeSchedule(
                    taker_rate=_fee_rate_from_bps(taker_bps, field="taker_base_fee"),
                    maker_rate=_fee_rate_from_bps(maker_bps, field="maker_base_fee")
                    if maker_bps is not None
                    else 0.0,
                    source="clob_market",
                )
        try:
            return VenueMarket(
                venue="polymarket",
                market_id=market_id,
                event_id=event_id,
                question=question,
                outcomes=outcomes,
                outcome_ids=outcome_ids,
                rules_text=rules_text,
                resolution_source=resolution_source,
                close_time=close_time,
                expected_settle_time=None,
                status=status,
                result=result,
                tick_size=tick_size,
                min_size=min_size,
                fee=fee,
                raw=gamma_item,
            )
        except ValueError as exc:
            # `VenueMarket`'s validators name the field but raise a BARE
            # `ValueError`, which is not a `VenueError` and so is outside
            # `app.services.scanner.VENUE_READ_FAULTS`: a single CLOB
            # `tick_size: 0` would abort a whole scan pass rather than
            # skipping this venue's listing (T44).
            raise VenuePayloadError(
                f"polymarket market {market_id} did not validate: {exc}",
                raw=gamma_item,
            ) from exc

    async def _fetch_gamma_markets(
        self, params: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        """`GET {gamma_api_url}/markets`, defensively parsed to `list[dict]`.

        A TARGETED lookup (`params` given, e.g. `condition_ids=...`) is one
        request. The UNFILTERED listing paginates, because Gamma's default
        page is 20 items and it silently returns exactly that.

        That default is why this matters. Before pagination existed this
        method was called with no params at all, so `list_markets()`
        returned **20 markets** — out of the thousands Polymarket lists —
        and reported success. `settings.scan_top_n` (default 200) bounds a
        list that could never reach 20, so raising it did nothing, and the
        Kalshi adapter next to it pages through up to 10,000. Every "no
        opportunities found" was a statement about 20 arbitrary markets,
        with nothing logged to say so.
        """
        if params is not None:
            return await self._fetch_gamma_page(params)

        collected: list[dict[str, Any]] = []
        for page in range(_MAX_GAMMA_PAGES):
            try:
                batch = await self._fetch_gamma_page(
                    {
                        "limit": str(_GAMMA_PAGE_LIMIT),
                        "offset": str(page * _GAMMA_PAGE_LIMIT),
                    }
                )
            except httpx.HTTPStatusError as exc:
                # Gamma answers an offset past its ceiling with 422. On a
                # LATER page that is the end of the listing, so keep what we
                # have and say so. On the FIRST page it is a real failure and
                # must not be mistaken for "the venue has no markets".
                if exc.response.status_code != 422 or page == 0:
                    raise
                logger.warning(
                    "polymarket",
                    extra={
                        "event": "gamma_offset_ceiling",
                        "page": page,
                        "markets": len(collected),
                        "detail": "gamma refused the next offset; listing ends here",
                    },
                )
                return collected
            collected.extend(batch)
            if len(batch) < _GAMMA_PAGE_LIMIT:
                return collected
        logger.warning(
            "polymarket",
            extra={
                "event": "gamma_market_page_cap_reached",
                "pages": _MAX_GAMMA_PAGES,
                "markets": len(collected),
                "detail": (
                    "listing truncated at the page cap; markets beyond it are "
                    "invisible to every scan"
                ),
            },
        )
        return collected

    async def _fetch_gamma_page(
        self, params: dict[str, str] | None
    ) -> list[dict[str, Any]]:
        """One `GET {gamma_api_url}/markets`, defensively parsed."""
        response = await self._gamma_client.get("/markets", params=params)
        response.raise_for_status()
        payload: object = response.json()
        items: object = payload.get("data", payload) if isinstance(payload, dict) else payload
        return _require_list_of_dicts(items, context="gamma /markets")

    async def _fetch_events(self) -> list[dict[str, Any]]:
        """`GET {gamma_api_url}/events`, paged and defensively parsed.

        Gamma's default page is 20 here exactly as it is on `/markets`,
        and this call was issued bare — so the event listing stopped at
        20 events however many existed, and every market outside them
        silently lost its `event_id`. Same defect, same fix, same page
        constants as the sibling listing; the 422-on-a-later-page
        handling is the same contract too (an offset past Gamma's
        ceiling is the end of the listing, not a failure).
        """
        collected: list[dict[str, Any]] = []
        for page in range(_MAX_GAMMA_PAGES):
            try:
                batch = await self._fetch_events_page(
                    {
                        "limit": str(_GAMMA_PAGE_LIMIT),
                        "offset": str(page * _GAMMA_PAGE_LIMIT),
                    }
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 422 or page == 0:
                    raise
                logger.warning(
                    "polymarket",
                    extra={
                        "event": "gamma_event_offset_ceiling",
                        "page": page,
                        "events": len(collected),
                        "detail": "gamma refused the next offset; listing ends here",
                    },
                )
                return collected
            collected.extend(batch)
            if len(batch) < _GAMMA_PAGE_LIMIT:
                return collected
        logger.warning(
            "polymarket",
            extra={
                "event": "gamma_event_page_cap_reached",
                "pages": _MAX_GAMMA_PAGES,
                "events": len(collected),
                "detail": (
                    "event listing truncated at the page cap; markets beyond "
                    "it lose their event_id and drop out of bundle grouping"
                ),
            },
        )
        return collected

    async def _fetch_events_page(
        self, params: dict[str, str] | None
    ) -> list[dict[str, Any]]:
        """One `GET {gamma_api_url}/events`, defensively parsed."""
        response = await self._gamma_client.get("/events", params=params)
        response.raise_for_status()
        payload: object = response.json()
        items: object = payload.get("data", payload) if isinstance(payload, dict) else payload
        return _require_list_of_dicts(items, context="gamma /events")

    async def _event_id_by_market_id(self) -> dict[str, str]:
        """Build `{market_id: event_id}` from Gamma `/events`'s nested markets.

        Standalone (non-grouped) markets simply do not appear as a key —
        callers treat a missing key as "no event grouping", not an error.

        MEMOIZED for `settings.polymarket_market_cache_ttl_s` (T38 F6).
        `GET /events` returns the whole listing and is identical no
        matter which market asked for it, but every `get_market()` —
        and therefore, before this, every single `get_book()` — issued
        one. A 400-book Polymarket pass fetched the full events listing
        400 times. See `_AsyncTtlMemo`.
        """
        return await self._events_memo.get("", self._build_event_id_by_market_id)

    @staticmethod
    def _event_id_from_item(item: dict[str, Any]) -> str | None:
        """Read a market's event id out of its OWN Gamma payload.

        Gamma embeds the market's parent event in every `/markets` item —
        measured present on 1,918 of 1,918 live open markets — so the
        grouping needs no second endpoint at all. Preferring it here
        fixes a defect that survived two other fixes: `GET /events`
        unfiltered returns CLOSED events, while `list_markets` returns
        OPEN markets, so the two listings were disjoint and the join
        still produced 0 of 1,918 tagged even once the key spaces and the
        pagination were both correct.

        A market belongs to one event in practice; `[0]` is that event.

        Returns:
            str | None: The event id, or `None` when the payload carries
                no usable grouping — the same "no event" contract
                `_event_id_by_market_id` documents.
        """
        events = item.get("events")
        if not isinstance(events, list):
            return None
        for event in events:
            if isinstance(event, dict) and event.get("id") is not None:
                return str(event["id"])
        return None

    async def _build_event_id_by_market_id(self) -> dict[str, str]:
        """Fetch Gamma `/events` and fold it into `{market_id: event_id}`.

        KEYED BY `conditionId`, because that is what `_market_id` returns
        and therefore the only key the caller will ever look up with.
        Gamma's nested markets carry BOTH ids: a numeric surrogate under
        `id` (`"239826"`) and the condition id under `conditionId`
        (`"0x064d33..."`). This previously preferred `id`, so the mapping
        and the lookup lived in different key spaces and the join could
        never hit — measured 0 of 1,918 live markets tagged, with the
        `or nested_market.get("conditionId")` fallback dead code because
        `id` is always present.
        """
        events = await self._fetch_events()
        mapping: dict[str, str] = {}
        for event in events:
            event_id_raw = event.get("id")
            if event_id_raw is None:
                continue
            nested = event.get("markets")
            if not isinstance(nested, list):
                continue
            for nested_market in nested:
                if not isinstance(nested_market, dict):
                    continue
                condition_id = nested_market.get("conditionId")
                if condition_id is not None:
                    mapping[str(condition_id)] = str(event_id_raw)
        return mapping

    async def _fetch_clob_markets_by_condition_id(self) -> dict[str, dict[str, Any]]:
        """`GET {clob_api_url}/markets` (single page), keyed by `condition_id`.

        This is an ENRICHMENT step: on any unexpected shape, it returns an
        empty mapping rather than raising, so `tick_size`/`min_size`/fee
        overrides simply fall back to their defaults instead of breaking
        the primary Gamma-sourced listing.
        """
        try:
            response = await self._clob_client.get(
                "/markets", params={"next_cursor": "MA=="}
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return {}
        payload: object = response.json()
        items: object = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            condition_id = item.get("condition_id")
            if condition_id is not None:
                result[str(condition_id)] = item
        return result

    async def _fetch_clob_market(self, condition_id: str) -> dict[str, Any] | None:
        """`GET {clob_api_url}/markets/{condition_id}`, or `None` on failure."""
        try:
            response = await self._clob_client.get(f"/markets/{condition_id}")
            response.raise_for_status()
        except httpx.HTTPError:
            return None
        payload: object = response.json()
        return payload if isinstance(payload, dict) else None

    # -- Order book -------------------------------------------------------

    async def get_book(self, market_id: str, outcome: str) -> OrderBook:
        """Fetch the current CLOB order book for one (market, outcome).

        Resolves `outcome` to its CLOB `token_id` via the market's
        metadata first (the book endpoint is keyed by `token_id`, not
        `market_id` + `outcome` — PLAN.md §3).

        THE BOOK ITSELF IS NEVER CACHED — `GET /book` is issued on every
        call, which is the entire point of a scan pass. What IS memoized
        (`settings.polymarket_market_cache_ttl_s`) is the market-metadata
        lookup that resolves the token id, because it costs THREE
        requests and a market's two outcomes need the identical answer.
        Before T38 that made one `get_book` four HTTP requests and a
        400-book pass ~1600 of them; it is now four for the first book
        of a market and one for every book after it, inside the TTL. See
        `_AsyncTtlMemo` for the full accounting.

        Args:
            market_id: Polymarket condition id.
            outcome: Outcome name, e.g. `"Yes"`/`"No"`.

        Returns:
            OrderBook: The normalized book snapshot.

        Raises:
            VenuePayloadError: If `outcome` is not one of the market's
                known outcomes, or the book payload is malformed
                (including a body that is not JSON at all, or a payload
                missing `bids`/`asks`).
            httpx.HTTPStatusError: If the CLOB answers the book request
                with an error status. Deliberately NOT flattened into
                `VenueError` — same rule Kalshi's `raise_for_venue_error`
                documents — so callers see the status. Every scanner
                read path treats it as a skippable venue fault
                (`app.services.scanner.VENUE_READ_FAULTS`).
        """
        market = await self._book_market_memo.get(
            market_id, lambda: self.get_market(market_id)
        )
        if not market.outcome_ids:
            # Distinguished from "you asked for the wrong outcome name"
            # because the cause and the fix are different: the market
            # parsed fine, Gamma just carried no `clobTokenIds` for it,
            # and the book endpoint is keyed by token id (T44).
            raise VenuePayloadError(
                f"market {market_id!r} carries no outcome -> token_id mapping "
                "(gamma 'clobTokenIds' was absent or empty), so its CLOB book "
                "cannot be addressed",
                raw=dict(market.raw),
            )
        token_id = _resolve_outcome_token(market.outcome_ids, outcome, market_id)
        response = await self._clob_client.get("/book", params={"token_id": token_id})
        response.raise_for_status()
        # A non-JSON body is the VENUE's fault, not a caller's — but
        # `response.json()` signals it with `json.JSONDecodeError`, a
        # `ValueError`, which no caller can catch without also catching
        # every genuine `ValueError` a programming error would raise.
        # Flattened here, at the adapter boundary, exactly as Kalshi's
        # `json_object` helper already does (T38 F2).
        try:
            payload: object = response.json()
        except ValueError as exc:
            raise VenuePayloadError(
                f"CLOB /book response was not JSON: {exc}", raw=response.text
            ) from exc
        if not isinstance(payload, dict):
            raise VenuePayloadError("CLOB /book payload was not an object", raw=payload)
        return _parse_book(payload, market_id=market_id, outcome=outcome)

    # -- Credentialed account data -----------------------------------------

    async def _ensure_clob_wrapper(self) -> ClobClientWrapper:
        """Lazily build and initialize the `ClobClientWrapper` for this adapter.

        Raises:
            VenueAuthError: If `settings.polymarket_private_key` is empty
                — checked BEFORE touching `ClobClientWrapper` at all, so
                this never makes a network call when uncredentialed.
        """
        if not settings.polymarket_private_key.get_secret_value():
            raise VenueAuthError(
                "POLYMARKET_PRIVATE_KEY is required for authenticated "
                "Polymarket calls"
            )
        if self._clob_wrapper is None:
            wrapper = ClobClientWrapper()
            # `ClobClientWrapper.initialize()` is declared `async def` but
            # its body is entirely synchronous (constructs `ClobClient`,
            # derives/sets API credentials — possibly signing a request)
            # (PLAN.md §3: "ClobClientWrapper methods are async def but
            # call the synchronous py_clob_client -> they block the event
            # loop"). `asyncio.to_thread` cannot be pointed at an
            # `async def` directly — it would call it in the worker
            # thread and get back an un-awaited coroutine object, never
            # running the body at all — so `_run_coroutine` drives it to
            # completion on its OWN event loop, inside the worker thread
            # `to_thread` provides, keeping the real loop unblocked.
            await asyncio.to_thread(self._run_coroutine, wrapper.initialize)
            self._clob_wrapper = wrapper
        return self._clob_wrapper

    @staticmethod
    def _run_coroutine(factory: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
        """Run a zero-arg async callable to completion on a fresh event loop.

        Only ever called from inside an `asyncio.to_thread` worker (see
        `_ensure_clob_wrapper`) — never on the real, surrounding event
        loop, which is the whole point.
        """
        return asyncio.run(factory())

    async def get_balance(self) -> Balance:
        """Fetch this account's Polymarket collateral (USDC) balance.

        Returns:
            Balance: `available` from the CLOB `/balance-allowance`
                endpoint's `balance` field; `locked` is always `0.0` —
                Polymarket's CLOB does not report a separate "locked"
                balance the way a per-order-margined exchange would.

        Raises:
            VenueAuthError: If `settings.polymarket_private_key` is empty.
            VenuePayloadError: If the payload is not an object, carries no
                `balance`, or carries one that is not a usable USD amount.
                It used to default to `0.0` on all three (T44), which
                reported a funded account as empty — a number a caller
                cannot tell from a real zero, on the field that decides
                how much capital exists. Kalshi's `get_balance` has always
                raised here; this is the same promise on both venues.
        """
        wrapper = await self._ensure_clob_wrapper()
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        raw: object = await asyncio.to_thread(wrapper.client.get_balance_allowance, params)
        if not isinstance(raw, dict):
            raise VenuePayloadError("balance-allowance payload was not an object", raw=raw)
        if "balance" not in raw:
            raise VenuePayloadError(
                "polymarket balance-allowance payload has no 'balance' field",
                raw=raw,
            )
        available = _try_float(raw["balance"])
        if available is None:
            raise VenuePayloadError(
                f"polymarket balance {raw['balance']!r} is not a number", raw=raw
            )
        try:
            return Balance(venue="polymarket", available=available, locked=0.0)
        except ValueError as exc:
            raise VenuePayloadError(
                f"polymarket balance {available!r} is not a usable USD amount: {exc}",
                raw=raw,
            ) from exc

    async def get_positions(self) -> list[Position]:
        """Fetch open Polymarket positions, derived from fill history.

        `py_clob_client` has no dedicated positions endpoint (verified
        against the installed package), so this nets every fill's
        BUY/SELL size into a running position per (market, asset). This
        is DERIVED, best-effort accounting, not an authoritative venue
        view — see the module docstring.

        Raises:
            VenueAuthError: If `settings.polymarket_private_key` is empty.
        """
        wrapper = await self._ensure_clob_wrapper()
        raw: object = await asyncio.to_thread(wrapper.client.get_trades, TradeParams())
        trades = _require_list_of_dicts(raw, context="get_trades")
        return _positions_from_trades(trades)

    async def get_open_orders(self) -> list[OrderAck]:
        """Fetch currently open (resting) Polymarket orders.

        Raises:
            VenueAuthError: If `settings.polymarket_private_key` is empty.
        """
        wrapper = await self._ensure_clob_wrapper()
        raw: object = await asyncio.to_thread(wrapper.client.get_orders, OpenOrderParams())
        items = _require_list_of_dicts(raw, context="get_orders")
        acks: list[OrderAck] = []
        unusable = 0
        for item in items:
            try:
                acks.append(_parse_order_ack(item))
            except (ValueError, VenuePayloadError) as exc:
                unusable += 1
                _log_skipped("order", str(exc))
        _require_not_all_dropped(
            items, kept=len(acks), unusable=unusable, context="polymarket get_orders"
        )
        return acks

    async def get_fills(self, since: datetime) -> list[Fill]:
        """Fetch Polymarket fills at/after `since`.

        Args:
            since: Aware UTC timestamp.

        Raises:
            VenueAuthError: If `settings.polymarket_private_key` is empty.
        """
        ensure_aware(since)
        wrapper = await self._ensure_clob_wrapper()
        raw: object = await asyncio.to_thread(wrapper.client.get_trades, TradeParams())
        items = _require_list_of_dicts(raw, context="get_trades")
        fills: list[Fill] = []
        parsed = 0
        unusable = 0
        for item in items:
            try:
                fill = _parse_fill(item)
            except (ValueError, VenuePayloadError) as exc:
                unusable += 1
                _log_skipped("fill", str(exc))
                continue
            parsed += 1
            if fill.ts >= since:
                fills.append(fill)
        _require_not_all_dropped(
            items, kept=parsed, unusable=unusable, context="polymarket get_trades"
        )
        return fills

    def fee_model(self) -> FeeModel:
        """Return Polymarket's `FeeModel`."""
        return PolymarketFeeModel()


# ---------------------------------------------------------------------------
# Module-level parsing helpers (shared by the adapter and, for book
# parsing, exercised directly by tests).
# ---------------------------------------------------------------------------


def _resolve_outcome_token(
    outcome_ids: Mapping[str, str], outcome: str, market_id: str
) -> str:
    """Resolve an outcome name to its CLOB token id.

    EXACT MATCH FIRST, then a case- and whitespace-insensitive fallback.
    That ordering is the whole design: a venue's own spelling always
    addresses exactly the token the venue paired with it, and folding is
    only ever a way to accept a caller who spelled the BINARY pair the
    way the rest of this kit spells it.

    Why the fallback exists at all. `KalshiAdapter.get_book` documents
    its `outcome` as case-INSENSITIVE, while this one resolved with a
    bare dict lookup — and Gamma spells its outcomes `"Yes"`/`"No"` in
    title case. So `get_book(mid, "YES")` fetched a book from one venue
    and raised on the other, for the canonical spelling that
    `app.strategies.base.normalize_outcome` says "every strategy in this
    kit hardcodes". Worse, it raised `VenuePayloadError`, which is inside
    `scanner.VENUE_READ_FAULTS` — so the caller got no error at all, just
    a `debug` log and a missing book that reads downstream as "no
    opportunity here". T21d recorded this same `"Yes"`-vs-`"YES"` split
    between resolving an outcome and keying on one as a money bug.

    Ambiguity is NOT resolved by guessing: two outcomes that differ only
    in case are a venue's business, and a fold that could mean either
    raises rather than picking one.

    Args:
        outcome_ids: The market's outcome -> token id mapping.
        outcome: The requested outcome name, in any casing.
        market_id: Condition id, for the error message only.

    Returns:
        str: The CLOB token id addressing that outcome's book.

    Raises:
        VenuePayloadError: If no outcome matches, or if only case
            distinguishes two that do.
    """
    if outcome in outcome_ids:
        return outcome_ids[outcome]
    folded = outcome.strip().casefold()
    matches = [name for name in outcome_ids if name.strip().casefold() == folded]
    if len(matches) == 1:
        return outcome_ids[matches[0]]
    if len(matches) > 1:
        raise VenuePayloadError(
            f"ambiguous outcome {outcome!r} for market {market_id!r}: it "
            f"case-folds onto {sorted(matches)}, which this adapter will not "
            "choose between",
            raw=dict(outcome_ids),
        )
    raise VenuePayloadError(
        f"unknown outcome {outcome!r} for market {market_id!r}; known "
        f"outcomes are {sorted(outcome_ids)}",
        raw=dict(outcome_ids),
    )


def _published_fee_schedule(gamma_item: dict[str, Any]) -> FeeSchedule | None:
    """Build a `FeeSchedule` from the rate Polymarket itself publishes.

    Every Gamma market carries `feesEnabled`, and most carry a
    `feeSchedule` object — `{"exponent": 1, "rate": 0.04, "takerOnly":
    true, "rebateRate": 0.25}`. That `rate` is the venue's own per-market
    answer, and it outranks any table maintained by hand here: the
    category table must guess from a `category` string that is often
    absent, which is why every one of 1,918 live markets fell through to
    its 0.05 unknown-category fallback while the venue was publishing
    0.03, 0.04, 0.05 and 0.07 — 82% of them wrong, and the 361 crypto
    markets wrong in the understating direction.

    REFUSES rather than guesses. `PolymarketFeeModel` computes
    `rate * p * (1 - p)`, i.e. exponent 1; a schedule declaring any other
    exponent describes a different curve, and applying its rate under
    this formula would be a confident wrong number. Same for a missing,
    non-numeric or out-of-range rate. In every such case this returns
    `None` and the caller keeps the table's answer AND the table's
    provenance.

    `takerOnly` is not consulted because the model already hard-codes the
    stronger claim that Polymarket makers pay nothing, and every one of
    the 1,776 published schedules agrees. `rebateRate` is deliberately
    NOT modelled: a rebate paid to makers would change which venue is
    worth quoting on, and inventing its mechanics would be inventing
    revenue.

    Args:
        gamma_item: One raw Gamma market payload.

    Returns:
        FeeSchedule | None: The venue's schedule with
            `source="venue_schedule"`, or `None` when the payload does
            not carry one this model can honor.
    """
    enabled = gamma_item.get("feesEnabled")
    if enabled is False:
        # An explicit "no fees here" is an answer, not an absence.
        return FeeSchedule(taker_rate=0.0, maker_rate=0.0,
                           source=_FEE_SOURCE_VENUE)
    schedule = gamma_item.get("feeSchedule")
    if not isinstance(schedule, dict):
        return None
    exponent = schedule.get("exponent")
    if exponent is not None and exponent != 1:
        return None
    rate = schedule.get("rate")
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        return None
    rate = float(rate)
    if not (math.isfinite(rate) and 0.0 <= rate <= 1.0):
        return None
    # Carried, never credited — see `FeeSchedule.maker_rebate_rate`.
    rebate = schedule.get("rebateRate")
    rebate_rate = 0.0
    if not isinstance(rebate, bool) and isinstance(rebate, (int, float)):
        candidate = float(rebate)
        if math.isfinite(candidate) and 0.0 <= candidate <= 1.0:
            rebate_rate = candidate
    return FeeSchedule(taker_rate=rate, maker_rate=0.0,
                       source=_FEE_SOURCE_VENUE,
                       maker_rebate_rate=rebate_rate)


def _market_id(item: dict[str, Any]) -> str | None:
    """Return a Gamma market item's condition id, preferring `conditionId`."""
    value = item.get("conditionId") or item.get("id")
    return str(value) if value is not None else None


def _require_list_of_dicts(value: object, *, context: str) -> list[dict[str, Any]]:
    """Validate `value` is a `list` of `dict`s, raising `VenuePayloadError` if not."""
    if not isinstance(value, list):
        raise VenuePayloadError(f"{context}: expected a list, got {value!r}", raw=value)
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise VenuePayloadError(f"{context}: expected a list of objects", raw=value)
        result.append(item)
    return result


def _log_skipped(kind: str, reason: str) -> None:
    """Log one skipped payload entry at WARNING (T44).

    Skipping a single malformed row is the right behaviour — one bad row
    must not cost the whole read — but doing it in silence is what makes
    a schema change undiagnosable. Carries the reason, never the payload.
    """
    logger.warning(
        "venue",
        extra={
            "event": f"polymarket_{kind}_entry_skipped",
            "venue": "polymarket",
            "reason": reason,
        },
    )


def _require_not_all_dropped(
    entries: list[dict[str, Any]], *, kept: int, unusable: int, context: str
) -> None:
    """Raise if the venue sent entries and NONE of them survived parsing.

    WHY (T44). Per-entry skipping is deliberately tolerant, but "every
    row failed" is not a bad row, it is a disagreement about the schema —
    and tolerating it yields an empty list, which
    `app.execution.reconcile` reads as "the venue holds nothing" and acts
    on (a read that RAISED it treats as "we do not know" and refuses to
    conclude from). A whole page of orders priced in cents, or under a
    renamed id key, would otherwise report every live order as gone.

    Args:
        entries: The raw entries the venue returned.
        kept: How many parsed successfully.
        unusable: How many failed to parse.
        context: Endpoint name, for the error message.

    Raises:
        VenuePayloadError: If `entries` is non-empty, nothing was kept,
            and every entry was unusable.
    """
    if entries and kept == 0 and unusable == len(entries):
        raise VenuePayloadError(
            f"{context}: the venue returned {len(entries)} entr"
            f"{'y' if len(entries) == 1 else 'ies'} and NONE parsed — reporting "
            "an empty result would be indistinguishable from the venue holding "
            "nothing",
            raw=entries,
        )


def _to_bool(value: object, *, field: str) -> bool:
    """Read a Gamma boolean flag that may arrive as a JSON bool or a string.

    `bool(value)` is NOT enough (T44): Gamma sends `resolved`/`closed` as
    real JSON booleans today, but `bool("false")` is `True`, so the day
    either arrives as a STRING every live market silently becomes
    `status="resolved"` — dropped from every scan, with nothing said.

    Args:
        value: The raw flag. `None`/absent reads as `False` (the flag is
            optional and its absence has always meant "not set").
        field: Field name, for the error message.

    Returns:
        bool: The flag.

    Raises:
        VenuePayloadError: If `value` is neither a boolean, nor `None`,
            nor a recognizable boolean spelling. Guessing is what caused
            the failure this exists to prevent.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes"):
            return True
        if text in ("false", "0", "no", ""):
            return False
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise VenuePayloadError(
        f"gamma market {field}: expected a boolean, got {value!r}", raw=value
    )


def _fee_rate_from_bps(value: object, *, field: str) -> float:
    """Convert a CLOB basis-point fee to the dimensionless rate.

    Args:
        value: The payload's fee value, in BASIS POINTS (module
            docstring: `taker_base_fee`/`maker_base_fee`/`fee_rate_bps`
            are bps, verified against the installed `py_clob_client`).
        field: Field name, for the error message.

    Returns:
        float: `value / 10_000`, e.g. `200` bps -> `0.02`.

    Raises:
        VenuePayloadError: If `value` is not a number, is negative, or
            lies strictly between 0 and 1. That last band is the
            unit-drift guard (T44): a value like `0.02` is what this
            field looks like once it is a RATE rather than bps, and
            dividing it by 10,000 again yields `0.000002` — a fee of
            effectively zero, which makes every marginal edge look
            profitable and is exactly the silent-wrong-number failure
            this venue has no other defence against. A whole `0` (an
            explicit fee waiver) and any bps value >= 1 are untouched.
    """
    bps = _try_float(value)
    if bps is None:
        raise VenuePayloadError(
            f"polymarket CLOB {field}: expected basis points, got {value!r}",
            raw=value,
        )
    if bps < 0.0:
        raise VenuePayloadError(
            f"polymarket CLOB {field}={value!r} is negative; fee rates are >= 0",
            raw=value,
        )
    if 0.0 < bps < 1.0:
        raise VenuePayloadError(
            f"polymarket CLOB {field}={value!r} is under one basis point. This "
            "field is BASIS POINTS (200 = 2%); a value in (0, 1) is a fee RATE, "
            f"and dividing it by 10,000 would report a fee of {bps / 10_000.0!r} "
            "— effectively free, on every fill",
            raw=value,
        )
    return bps / 10_000.0


def _parse_list_field(value: object) -> list[Any]:
    """Parse a Gamma field that may be a real list OR a JSON-encoded string.

    Gamma has historically encoded `outcomes`/`clobTokenIds` as a JSON
    string (e.g. `'["Yes", "No"]'`) rather than a native JSON array; this
    accepts either.
    """
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise VenuePayloadError(
                f"could not parse JSON list field: {value!r}", raw=value
            ) from exc
        if not isinstance(parsed, list):
            raise VenuePayloadError(f"expected a JSON list, got {parsed!r}", raw=value)
        return parsed
    if isinstance(value, list):
        return value
    raise VenuePayloadError(
        f"expected a list or JSON-encoded list, got {value!r}", raw=value
    )



def _clob_size_field(
    clob_item: Mapping[str, Any],
    spellings: tuple[str, ...],
    *,
    market_id: str,
    label: str,
) -> float:
    """Read a CLOB-market numeric field, trying each spelling in order.

    THE FIRST SPELLING IS THE ONE THE LIVE API ACTUALLY SENDS. The
    `/markets` payload uses `minimum_tick_size` and `minimum_order_size`;
    the `/book` payload uses the shorter `tick_size` / `min_order_size`
    for the same quantities. This adapter read the BOOK's spellings out
    of the MARKET payload, so both lookups missed on every real market
    and silently took their defaults -- `tick_size=0.01` and
    `min_size=0.0`.

    Measured against 1000 live markets (2026-09-06): `minimum_tick_size`
    and `minimum_order_size` were present in 1000/1000, while
    `tick_size` and `min_order_size` appeared in 0/1000. The real values
    are not the defaults either: minimum order size was 15 on 962 of
    them and 5 on 34, against a default of 0.0, and 32 markets carried a
    tick of 0.001 or 0.04 against a default of 0.01.

    Both defaults are wrong in the dangerous direction. A tick 10x too
    coarse rounds prices past an edge that settlement strategies measure
    in fractions of a cent, and a minimum order size of 0.0 lets sizing
    propose an order the venue will simply refuse.

    Absence is therefore LOUD rather than defaulted: every real payload
    carries these, so a missing one means the contract changed and a
    guessed number would be priced. The raise is a `VenuePayloadError`,
    which `scanner.VENUE_READ_FAULTS` already skips per market rather
    than aborting a pass.

    Args:
        clob_item: The CLOB `/markets` payload for one market.
        spellings: Field names to try, live spelling first.
        market_id: For the error message.
        label: Human name of the quantity, for the error message.

    Returns:
        float: The field's value.

    Raises:
        VenuePayloadError: If no spelling is present, or the value is
            not numeric.
    """
    for name in spellings:
        if name not in clob_item:
            continue
        value = clob_item[name]
        try:
            return float(cast(Any, value))
        except (TypeError, ValueError) as exc:
            raise VenuePayloadError(
                f"polymarket market {market_id}: {label} field {name!r} is "
                f"{value!r}, which is not a number",
                raw=clob_item,
            ) from exc
    raise VenuePayloadError(
        f"polymarket market {market_id}: CLOB payload carries no {label} under "
        f"any of {list(spellings)!r}; refusing to substitute a default, because "
        f"a wrong {label} is priced silently",
        raw=clob_item,
    )

def _to_float(value: object, *, default: float) -> float:
    """Best-effort `float(value)`, falling back to `default` on failure/`None`."""
    if value is None:
        return default
    try:
        return float(cast(Any, value))
    except (TypeError, ValueError):
        return default


def _try_float(value: object) -> float | None:
    """Best-effort `float(value)`, returning `None` on failure/`None`."""
    if value is None:
        return None
    try:
        return float(cast(Any, value))
    except (TypeError, ValueError):
        return None


def _parse_iso8601(value: str) -> datetime:
    """Parse an ISO-8601 timestamp (optionally `Z`-suffixed) to aware UTC."""
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
        return ensure_aware(parsed)
    except (ValueError, TypeError) as exc:
        raise VenuePayloadError(f"could not parse timestamp: {value!r}", raw=value) from exc


def _infer_result(outcomes: tuple[str, ...], raw_prices: object) -> str | None:
    """Best-effort inference of the winning outcome from `outcomePrices`.

    Not pinned by PLAN.md §3 or the brief; Gamma does not cleanly expose
    a `result` field in the fields this adapter otherwise maps. Returns
    `None` (rather than guessing) whenever the prices don't parse or
    don't line up 1:1 with `outcomes`.
    """
    if raw_prices is None:
        return None
    try:
        prices = _parse_list_field(raw_prices)
        floats = [float(p) for p in prices]
    except (VenuePayloadError, TypeError, ValueError):
        return None
    if not floats or len(floats) != len(outcomes):
        return None
    best_index = max(range(len(floats)), key=lambda i: floats[i])
    return outcomes[best_index]


def _gamma_updated_at(raw: Mapping[str, Any]) -> datetime | None:
    """Best-effort parse of a Gamma market's `updatedAt`, or `None`."""
    value = raw.get("updatedAt")
    if not isinstance(value, str):
        return None
    try:
        return _parse_iso8601(value)
    except VenuePayloadError:
        return None


def _parse_book(payload: dict[str, Any], *, market_id: str, outcome: str) -> OrderBook:
    """Parse a CLOB `/book` payload into a normalized `OrderBook`.

    Args:
        payload: The raw CLOB book response (PLAN.md §3 shape:
            `{bids, asks, market, asset_id, timestamp(ms), hash,
            min_order_size, tick_size, neg_risk, last_trade_price}`).
        market_id: The Polymarket condition id this book belongs to.
        outcome: The outcome name this book belongs to.

    Returns:
        OrderBook: `bids`/`asks` price/size decimal strings converted to
            `float`; `timestamp` converted to aware UTC — MILLISECONDS as
            PLAN.md §3 pins it, but a seconds-valued epoch is read as
            seconds rather than divided by 1000 again (T44: `1767225600`
            treated as milliseconds dates the book to 1970-01-21, a
            56-year-old snapshot produced in silence). Milliseconds are
            distinguished by magnitude exactly as `_try_epoch` and
            Kalshi's `_parse_timestamp` already do; a seconds epoch above
            `1e12` would be the year 33658.
        The payload's OWN `tick_size`/`min_order_size`/`neg_risk`/
            `last_trade_price`/`hash` are recorded on `metadata` (NOT on
            `OrderBook`, which has no such fields by design — those live
            on `VenueMarket`, populated separately in `_build_market`).

    Raises:
        VenuePayloadError: If `bids`/`asks` are missing or malformed, or
            `timestamp` is absent/unparseable.
    """
    if "bids" not in payload or "asks" not in payload:
        raise VenuePayloadError("CLOB book payload missing bids/asks", raw=payload)
    raw_bids = payload["bids"]
    raw_asks = payload["asks"]
    if not isinstance(raw_bids, list) or not isinstance(raw_asks, list):
        raise VenuePayloadError("CLOB book bids/asks were not lists", raw=payload)
    try:
        bids = [
            BookLevel(price=float(lvl["price"]), size=float(lvl["size"]))
            for lvl in raw_bids
        ]
        asks = [
            BookLevel(price=float(lvl["price"]), size=float(lvl["size"]))
            for lvl in raw_asks
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise VenuePayloadError(f"malformed CLOB book payload: {exc}", raw=payload) from exc
    ts = _try_epoch(payload.get("timestamp"))
    if ts is None:
        raise VenuePayloadError(
            f"CLOB book payload has no parseable 'timestamp' "
            f"(got {payload.get('timestamp')!r}); a book with no read time cannot "
            "be aged, and inventing one would hide a stale quote",
            raw=payload,
        )
    metadata: dict[str, Any] = {
        "tick_size": payload.get("tick_size"),
        "min_order_size": payload.get("min_order_size"),
        "neg_risk": payload.get("neg_risk"),
        "last_trade_price": payload.get("last_trade_price"),
        "hash": payload.get("hash"),
    }
    return OrderBook(
        venue="polymarket",
        market_id=market_id,
        outcome=outcome,
        bids=tuple(bids),
        asks=tuple(asks),
        ts=ts,
        metadata=metadata,
    )


def _positions_from_trades(trades: list[dict[str, Any]]) -> list[Position]:
    """Net raw CLOB fills into best-effort positions per (market, asset_id).

    A negative net (a short) is impossible on Polymarket (PLAN.md §3: no
    naked shorts on either supported venue), so it is dropped rather than
    reported — a negative net here means the derivation missed an earlier
    fill (e.g. pagination), not a real short position.

    `Position.outcome` is set to the CLOB `asset_id` (token id), not an
    outcome NAME (`"Yes"`/`"No"`) — raw trades carry no outcome name, only
    the token id, and cross-referencing it back to a name would require
    an extra `get_market` call per distinct market seen here. Documented
    limitation, not exercised by this kit's tests.

    THE DERIVED AVERAGE PRICE IS NO LONGER CLAMPED (T44). It used to be
    `min(max(cost_basis / net_size, 0.0), 1.0)`, so a trades payload
    priced in cents (`42`) — or carrying any other out-of-domain price —
    produced a position at `avg_price=1.0`: a real-looking cost basis of
    $1.00 per contract, on a position that is used for sizing, with
    nothing raised and nothing logged. Only IEEE-754 dust at the
    boundaries is absorbed now; a materially out-of-range average is a
    `VenuePayloadError` naming the venue, the field and the range.

    Raises:
        VenuePayloadError: If a netted average price is outside `[0,1]`,
            or if every trade was unusable (which would otherwise report
            a funded account as flat).
    """
    #: Absolute tolerance for the netted average price at the [0,1]
    #: boundaries. `cost_basis / net_size` is a weighted mean of prices
    #: that are each already in `[0,1]`, so in exact arithmetic it cannot
    #: leave the range; chained float rounding can put it a few ULPs
    #: outside, and that dust is absorbed rather than reported as a bad
    #: payload. Many orders of magnitude below any real price error.
    epsilon = 1e-9
    totals: dict[tuple[str, str], list[float]] = {}
    unusable = 0
    for trade in trades:
        market = trade.get("market")
        asset_id = trade.get("asset_id")
        side = str(trade.get("side", "")).upper()
        price = _try_float(trade.get("price"))
        size = _try_float(trade.get("size"))
        if market is None or asset_id is None or price is None or size is None:
            unusable += 1
            _log_skipped("trade", "market/asset_id/price/size missing or non-numeric")
            continue
        if side not in ("BUY", "SELL"):
            # `side if "BUY" else -size` used to net ANY unrecognized
            # side (including an absent one) as a SELL, which silently
            # cancels the buy it should have added and can erase a real
            # position entirely (T44).
            unusable += 1
            _log_skipped("trade", f"side {trade.get('side')!r} is neither BUY nor SELL")
            continue
        key = (str(market), str(asset_id))
        signed = size if side == "BUY" else -size
        entry = totals.setdefault(key, [0.0, 0.0])
        entry[0] += signed
        entry[1] += signed * price
    positions: list[Position] = []
    for (market, asset_id), (net_size, cost_basis) in totals.items():
        if net_size <= 0:
            continue
        avg_price = cost_basis / net_size
        if -epsilon <= avg_price < 0.0:
            avg_price = 0.0
        elif 1.0 < avg_price <= 1.0 + epsilon:
            avg_price = 1.0
        try:
            positions.append(
                Position(
                    venue="polymarket",
                    market_id=market,
                    outcome=asset_id,
                    size=net_size,
                    avg_price=avg_price,
                )
            )
        except ValueError as exc:
            raise VenuePayloadError(
                f"polymarket position in market {market!r} netted an average price "
                f"of {avg_price!r} from its trades, which is not a probability in "
                f"[0,1] ({exc}). Trade prices are probabilities; a payload in "
                "cents would look exactly like this",
                raw=trades,
            ) from exc
    _require_not_all_dropped(
        trades, kept=len(positions), unusable=unusable, context="polymarket positions"
    )
    return positions


def _parse_order_ack(raw: dict[str, Any]) -> OrderAck:
    """Best-effort parse of one raw `get_orders` entry into an `OrderAck`.

    `py_clob_client.get_orders` returns a plain `list[dict]` with no
    pinned schema (PLAN.md §3 does not cover it); key names below are a
    best-effort guess, not a verified contract.

    OPTIONAL STAYS OPTIONAL, PRESENT-BUT-WRONG DOES NOT (T44). An absent
    `size_matched`/`original_size`/`price` still falls back exactly as
    before — those fields are not guaranteed. But a field that IS present
    and does not parse used to fall back too: `_try_float(...) or 0.0`
    turned `"size_matched": "abc"` into `0.0`, reporting a partly-filled
    order as untouched, and an unparseable `price` into "nothing has
    filled yet". Those are now `VenuePayloadError`s naming the field.
    `get_open_orders` skips a single such entry and logs it, and raises
    if EVERY entry failed.

    Raises:
        VenuePayloadError: If the entry carries no order id, or a
            present numeric field is unparseable, or the resulting values
            are outside `OrderAck`'s domain (a `price` of `42` is what a
            cents-encoded payload looks like).
    """
    order_id = str(raw.get("id") or raw.get("order_id") or "")
    if not order_id:
        # Kalshi's `parse_order_ack` has always refused this. An ack with
        # an empty id is indexed under "" by `app.execution.reconcile`
        # and can be matched to the wrong order.
        raise VenuePayloadError(
            "polymarket order payload carries neither 'id' nor 'order_id'", raw=raw
        )
    size_matched = _present_float(raw, "size_matched", default=0.0)
    original_size = _present_float(raw, "original_size", default=None)
    if original_size is None:
        original_size = _present_float(raw, "size", default=size_matched)
    remaining = max(original_size - size_matched, 0.0)
    avg_fill_price = _present_float(raw, "price", default=None)
    status_raw = str(raw.get("status", "open")).lower()
    if status_raw not in _ORDER_ACK_STATUSES:
        logger.warning(
            "order",
            extra={
                "event": "polymarket_unknown_order_status",
                "venue": "polymarket",
                "order_id": order_id,
                "status": status_raw,
                "assumed": "open",
            },
        )
    status = status_raw if status_raw in _ORDER_ACK_STATUSES else "open"
    ts = _try_epoch(raw.get("created_at") or raw.get("timestamp")) or utcnow()
    try:
        return OrderAck(
            venue="polymarket",
            order_id=order_id,
            client_order_id=str(raw.get("client_order_id") or order_id),
            status=cast(_OrderAckStatus, status),
            filled_size=size_matched,
            remaining_size=remaining,
            avg_fill_price=avg_fill_price,
            ts=ts,
        )
    except ValueError as exc:
        raise VenuePayloadError(
            f"polymarket order {order_id} did not validate: {exc}", raw=raw
        ) from exc


@overload
def _present_float(raw: dict[str, Any], key: str, *, default: float) -> float: ...


@overload
def _present_float(raw: dict[str, Any], key: str, *, default: None) -> float | None: ...


def _present_float(
    raw: dict[str, Any], key: str, *, default: float | None
) -> float | None:
    """Return `raw[key]` as a float; `default` only when the key is ABSENT.

    The distinction is the whole point (T44): a missing optional field
    keeps its documented fallback, while a field the venue DID send and
    we cannot read is a disagreement, not a zero.

    Raises:
        VenuePayloadError: If `key` is present but not a finite number.
    """
    if key not in raw or raw[key] is None:
        return default
    parsed = _try_float(raw[key])
    if parsed is None:
        raise VenuePayloadError(
            f"polymarket order field {key}={raw[key]!r} is not a number", raw=raw
        )
    return parsed


def _parse_fill(raw: dict[str, Any]) -> Fill:
    """Best-effort parse of one raw `get_trades` entry into a `Fill`.

    Fee is reconstructed from the pinned Polymarket formula (PLAN.md §3:
    `fee = size * rate * price * (1 - price)`) using the trade's own
    `fee_rate_bps` when present (basis points, same convention as
    `taker_base_fee` — see the module docstring), rather than trusting an
    unpinned dollar-fee field. Every fill is reported `liquidity="taker"`
    (a conservative simplification: a mislabeled maker fill would be
    OVER-, never under-, charged here, since Polymarket makers pay $0 —
    PLAN.md §3 — and this never applies a maker rate). Not exercised by
    this kit's tests (GUARDRAILS.md §1.4 forbids the network access that
    would be needed to observe a real payload).

    AN ABSENT `fee_rate_bps` IS AN ESTIMATE, NOT A ZERO (T44). It used to
    produce `fee=$0.00`, silently, for exactly the reason Kalshi's
    `_parse_fill` refuses to: "a fabricated zero fee is exactly the input
    that makes a marginal edge look profitable". The conservative
    category-table rate is applied instead, and which of the two happened
    is recorded in `Fill.metadata["fee_source"]` (`"venue_rate"` when the
    trade carried its own bps, `"category_estimate"` when it did not) —
    the same contract Kalshi's fills already carry.

    A FILL WITH NO PARSEABLE TIMESTAMP IS DROPPED, not stamped `now()`:
    `get_fills` filters on `ts >= since`, so an invented "now" makes an
    old fill look like a new one. Kalshi has always dropped these.

    Raises:
        VenuePayloadError: If `price`/`size` are missing or unparseable,
            if no timestamp parses, if `fee_rate_bps` is not in basis
            points, or if the values are outside `Fill`'s domain.
    """
    price = _try_float(raw.get("price"))
    size = _try_float(raw.get("size"))
    if price is None or size is None:
        raise VenuePayloadError("trade payload missing price/size", raw=raw)
    fee_rate_bps = raw.get("fee_rate_bps")
    if fee_rate_bps is None:
        schedule = category_fee_schedule(None)
        fee_source = "category_estimate"
    else:
        schedule = FeeSchedule(
            taker_rate=_fee_rate_from_bps(fee_rate_bps, field="fee_rate_bps"),
            maker_rate=0.0,
            source="clob_market",
        )
        fee_source = "venue_rate"
    liquidity: Liquidity = "taker"
    fee = PolymarketFeeModel().fee(
        price=price, size_contracts=size, liquidity=liquidity, schedule=schedule
    )
    order_id = str(raw.get("taker_order_id") or raw.get("id") or "")
    ts = _try_epoch(raw.get("match_time") or raw.get("last_update") or raw.get("timestamp"))
    if ts is None:
        raise VenuePayloadError(
            "trade payload has no parseable timestamp (match_time/last_update/"
            "timestamp); it cannot be placed on either side of a `since` bound",
            raw=raw,
        )
    try:
        return Fill(
            venue="polymarket",
            order_id=order_id,
            price=price,
            size=size,
            fee=fee,
            ts=ts,
            liquidity=liquidity,
            metadata={
                "market": raw.get("market"),
                "asset_id": raw.get("asset_id"),
                "fee_source": fee_source,
            },
        )
    except ValueError as exc:
        raise VenuePayloadError(
            f"polymarket trade did not validate: {exc}", raw=raw
        ) from exc


def _try_epoch(value: object) -> datetime | None:
    """Best-effort parse of an epoch timestamp (seconds OR milliseconds)."""
    parsed = _try_float(value)
    if parsed is None:
        return None
    seconds = parsed / 1000.0 if parsed > 1e12 else parsed
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
