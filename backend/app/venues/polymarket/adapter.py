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
import time
from collections.abc import Callable, Coroutine, Mapping
from datetime import UTC, datetime
from typing import Any, Generic, Literal, TypeVar, cast

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
from app.venues.base import BaseAdapter, FeeModel, VenueAuthError, VenuePayloadError
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

#: `OrderAck.status`'s literal type, named here so parsers can `cast` a
#: validated `str` back to it without reaching for `Any`.
_OrderAckStatus = Literal["open", "filled", "partially_filled", "cancelled", "rejected"]
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
        """
        loop = asyncio.get_running_loop()
        if self._lock_loop is not loop:
            self._lock_loop = loop
            self._locks = {}
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _store(self, key: str, value: _T) -> None:
        """Cache `value` under `key`, evicting expired entries when over cap."""
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
        # An unheld lock is safe to drop: the worst case is that a
        # newcomer builds a fresh one and performs one redundant fetch.
        self._locks = {
            lock_key: lock
            for lock_key, lock in self._locks.items()
            if lock.locked()
        }

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
        self._gamma_client = httpx.AsyncClient(
            base_url=settings.gamma_api_url, transport=transport, timeout=30.0
        )
        self._clob_client = httpx.AsyncClient(
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
        #: resolution ONLY, so a market's two outcomes cost one lookup
        #: between them instead of one each. `get_market()` itself stays
        #: UNMEMOIZED: it is a public read whose callers (the matcher,
        #: the links API) are entitled to a fresh answer, and nothing
        #: about F6 is their fault.
        self._book_market_memo: _AsyncTtlMemo[VenueMarket] = _AsyncTtlMemo(ttl_s)

    async def aclose(self) -> None:
        """Close both underlying `httpx.AsyncClient` instances."""
        await self._gamma_client.aclose()
        await self._clob_client.aclose()

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
        markets = [
            self._build_market(
                item,
                event_id_by_market.get(_market_id(item) or ""),
                clob_by_condition_id.get(_market_id(item) or ""),
            )
            for item in gamma_items
        ]
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
        return self._build_market(match, event_id_by_market.get(market_id), clob_item)

    def _build_market(
        self,
        gamma_item: dict[str, Any],
        event_id: str | None,
        clob_item: dict[str, Any] | None,
    ) -> VenueMarket:
        """Build a `VenueMarket` from a Gamma item plus optional CLOB enrichment."""
        market_id = _market_id(gamma_item)
        if market_id is None:
            raise VenuePayloadError(
                "gamma market payload missing id/conditionId", raw=gamma_item
            )
        question = str(gamma_item.get("question", ""))
        outcomes = tuple(str(o) for o in _parse_list_field(gamma_item.get("outcomes", [])))
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
        resolved = bool(gamma_item.get("resolved", False))
        closed = bool(gamma_item.get("closed", False))
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
        fee = category_fee_schedule(category_str)
        tick_size = 0.01
        min_size = 0.0
        if clob_item is not None:
            tick_size = _to_float(clob_item.get("tick_size"), default=tick_size)
            min_size = _to_float(clob_item.get("min_order_size"), default=min_size)
            taker_bps = clob_item.get("taker_base_fee")
            if taker_bps is not None:
                maker_bps = clob_item.get("maker_base_fee")
                fee = FeeSchedule(
                    taker_rate=float(cast(Any, taker_bps)) / 10_000.0,
                    maker_rate=float(cast(Any, maker_bps)) / 10_000.0
                    if maker_bps is not None
                    else 0.0,
                    source="clob_market",
                )
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

    async def _fetch_gamma_markets(
        self, params: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        """`GET {gamma_api_url}/markets`, defensively parsed to `list[dict]`."""
        response = await self._gamma_client.get("/markets", params=params)
        response.raise_for_status()
        payload: object = response.json()
        items: object = payload.get("data", payload) if isinstance(payload, dict) else payload
        return _require_list_of_dicts(items, context="gamma /markets")

    async def _fetch_events(self) -> list[dict[str, Any]]:
        """`GET {gamma_api_url}/events`, defensively parsed to `list[dict]`."""
        response = await self._gamma_client.get("/events")
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

    async def _build_event_id_by_market_id(self) -> dict[str, str]:
        """Fetch Gamma `/events` and fold it into `{market_id: event_id}`."""
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
                nested_id = nested_market.get("id") or nested_market.get("conditionId")
                if nested_id is not None:
                    mapping[str(nested_id)] = str(event_id_raw)
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
        try:
            token_id = market.outcome_ids[outcome]
        except KeyError:
            raise VenuePayloadError(
                f"unknown outcome {outcome!r} for market {market_id!r}",
                raw=dict(market.outcome_ids),
            ) from None
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
        """
        wrapper = await self._ensure_clob_wrapper()
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        raw: object = await asyncio.to_thread(wrapper.client.get_balance_allowance, params)
        if not isinstance(raw, dict):
            raise VenuePayloadError("balance-allowance payload was not an object", raw=raw)
        available = _to_float(raw.get("balance"), default=0.0)
        return Balance(venue="polymarket", available=available, locked=0.0)

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
        for item in items:
            try:
                acks.append(_parse_order_ack(item))
            except ValueError:
                continue
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
        for item in items:
            try:
                fill = _parse_fill(item)
            except ValueError:
                continue
            if fill.ts >= since:
                fills.append(fill)
        return fills

    def fee_model(self) -> FeeModel:
        """Return Polymarket's `FeeModel`."""
        return PolymarketFeeModel()


# ---------------------------------------------------------------------------
# Module-level parsing helpers (shared by the adapter and, for book
# parsing, exercised directly by tests).
# ---------------------------------------------------------------------------


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
            `float`; `timestamp` (ms) converted to aware UTC; the
            payload's OWN `tick_size`/`min_order_size`/`neg_risk`/
            `last_trade_price`/`hash` recorded on `metadata` (NOT on
            `OrderBook`, which has no such fields by design — those live
            on `VenueMarket`, populated separately in `_build_market`).

    Raises:
        VenuePayloadError: If `bids`/`asks` are missing or malformed.
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
        ts_ms = int(payload["timestamp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise VenuePayloadError(f"malformed CLOB book payload: {exc}", raw=payload) from exc
    ts = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC)
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
    """
    totals: dict[tuple[str, str], list[float]] = {}
    for trade in trades:
        market = trade.get("market")
        asset_id = trade.get("asset_id")
        side = str(trade.get("side", "")).upper()
        price = _try_float(trade.get("price"))
        size = _try_float(trade.get("size"))
        if market is None or asset_id is None or price is None or size is None:
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
        avg_price = min(max(cost_basis / net_size, 0.0), 1.0)
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
        except ValueError:
            continue
    return positions


def _parse_order_ack(raw: dict[str, Any]) -> OrderAck:
    """Best-effort parse of one raw `get_orders` entry into an `OrderAck`.

    `py_clob_client.get_orders` returns a plain `list[dict]` with no
    pinned schema (PLAN.md §3 does not cover it); key names below are a
    best-effort guess, not a verified contract.
    """
    order_id = str(raw.get("id") or raw.get("order_id") or "")
    size_matched = _try_float(raw.get("size_matched")) or 0.0
    original_size = (
        _try_float(raw.get("original_size")) or _try_float(raw.get("size")) or size_matched
    )
    remaining = max(original_size - size_matched, 0.0)
    avg_fill_price = _try_float(raw.get("price"))
    status_raw = str(raw.get("status", "open")).lower()
    status = status_raw if status_raw in _ORDER_ACK_STATUSES else "open"
    ts = _try_epoch(raw.get("created_at") or raw.get("timestamp")) or utcnow()
    return OrderAck(
        venue="polymarket",
        order_id=order_id,
        client_order_id=str(raw.get("client_order_id") or order_id) or order_id or "unknown",
        status=cast(_OrderAckStatus, status),
        filled_size=size_matched,
        remaining_size=remaining,
        avg_fill_price=avg_fill_price,
        ts=ts,
    )


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
    """
    price = _try_float(raw.get("price"))
    size = _try_float(raw.get("size"))
    if price is None or size is None:
        raise ValueError("trade payload missing price/size")
    fee_rate_bps = _try_float(raw.get("fee_rate_bps")) or 0.0
    schedule = FeeSchedule(
        taker_rate=fee_rate_bps / 10_000.0, maker_rate=0.0, source="clob_market"
    )
    liquidity: Liquidity = "taker"
    fee = PolymarketFeeModel().fee(
        price=price, size_contracts=size, liquidity=liquidity, schedule=schedule
    )
    order_id = str(raw.get("taker_order_id") or raw.get("id") or "")
    ts = _try_epoch(raw.get("match_time") or raw.get("last_update") or raw.get("timestamp"))
    return Fill(
        venue="polymarket",
        order_id=order_id,
        price=price,
        size=size,
        fee=fee,
        ts=ts or utcnow(),
        liquidity=liquidity,
        metadata={"market": raw.get("market"), "asset_id": raw.get("asset_id")},
    )


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
