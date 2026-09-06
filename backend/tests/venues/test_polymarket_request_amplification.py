"""T38 F6 -- how many HTTP requests one Polymarket `get_book` really costs.

THE FINDING. The CLOB book endpoint is keyed by `token_id`, not by
`(market_id, outcome)`, so `PolymarketAdapter.get_book` resolves the
outcome through `get_market()` first -- and `get_market()` is itself
THREE requests: `GET gamma/markets?condition_ids=...`, `GET gamma/events`
(the FULL events listing, only so the market can be tagged with an
`event_id` `get_book` never reads), and `GET clob/markets/{condition_id}`.
One `get_book` was therefore FOUR requests, not one, and none of the
three extra ones was cached: a `scan()` pass over `scan_top_n=200`
markets x 2 outcomes issued ~1600 Polymarket requests for 400 books, 400
of them identical full events listings, and one duplicate market lookup
per outcome. Kalshi's `get_book` is exactly one request, so the same pass
also made Polymarket's leg run several times longer than Kalshi's --
stretching the very fetch window `settings.scan_book_fetch_concurrency`
exists to shrink.

`settings.scan_book_fetch_concurrency`'s own comment justifies its bound
as a rate-limit budget. That bound was never WRONG as a rate (the four
requests inside one `get_book` are sequential, so at most
`scan_book_fetch_concurrency` are ever in flight) -- but it was counting
`get_book` CALLS while its comment reasoned about REQUESTS, and those
differed by 4x.

These tests measure the request count directly, with a counting
`httpx.MockTransport`. GUARDRAILS.md §1.4: no venue is contacted -- the
transport answers every request from the hand-written fixtures under
`tests/fixtures/polymarket/`, and it also records what was asked for.
"""
import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.venues.polymarket import adapter as adapter_module
from app.venues.polymarket.adapter import PolymarketAdapter

FIXTURES = Path(__file__).parent.parent / "fixtures" / "polymarket"


def _load(name: str) -> Any:
    with open(FIXTURES / name) as f:
        return json.load(f)


GAMMA_MARKETS: list[dict[str, Any]] = _load("gamma_markets.json")
CLOB_BOOK: dict[str, Any] = _load("clob_book.json")
CLOB_MARKET: dict[str, Any] = _load("clob_market.json")

MARKET_A001 = "0x0000000000000000000000000000000000000000000000000000000000a001"
MARKET_A002 = "0x0000000000000000000000000000000000000000000000000000000000a002"


class CountingTransport(httpx.MockTransport):
    """`MockTransport` that tallies requests by `{host}{path}`.

    Attributes:
        counts: `Counter` of `"gamma-api.polymarket.com/events"`-style
            keys. Every entry is a request that WOULD have gone to a
            venue; nothing leaves the process.
    """

    def __init__(self) -> None:
        """Route Gamma/CLOB paths to the fixtures, counting as it goes."""
        self.counts: Counter[str] = Counter()
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.counts[f"{url.host}{url.path}"] += 1
        if url.host == "gamma-api.polymarket.com":
            if url.path == "/markets":
                condition_ids = url.params.get("condition_ids")
                if condition_ids:
                    return httpx.Response(
                        200,
                        json=[
                            m
                            for m in GAMMA_MARKETS
                            if m.get("conditionId") == condition_ids
                        ],
                    )
                return httpx.Response(200, json=GAMMA_MARKETS)
            if url.path == "/events":
                return httpx.Response(200, json=[])
        elif url.host == "clob.polymarket.com":
            if url.path == "/book":
                return httpx.Response(200, json=CLOB_BOOK)
            if url.path == "/markets":
                return httpx.Response(
                    200, json={"data": [CLOB_MARKET], "next_cursor": "LTE="}
                )
            if url.path.startswith("/markets/"):
                return httpx.Response(200, json=CLOB_MARKET)
        return httpx.Response(404, json={"error": "unhandled", "url": str(url)})

    @property
    def total(self) -> int:
        """Every request this transport was asked for, across all paths."""
        return sum(self.counts.values())


@pytest.mark.asyncio
async def test_one_get_book_costs_four_requests_the_first_time() -> None:
    """The amplification itself, stated as the number it is.

    This is the measurement the finding rests on, asserted exactly:
    one `get_book` is one `/book` call plus the three-request
    `get_market()` resolution behind it. It is NOT a bug to fix by
    removing the lookup -- the book endpoint genuinely needs a
    `token_id` -- which is why the fix is to stop REPEATING it.
    """
    transport = CountingTransport()
    adapter = PolymarketAdapter(transport=transport)

    await adapter.get_book(MARKET_A001, "Yes")

    assert transport.counts["clob.polymarket.com/book"] == 1
    assert transport.counts["gamma-api.polymarket.com/markets"] == 1
    assert transport.counts["gamma-api.polymarket.com/events"] == 1
    assert transport.counts[f"clob.polymarket.com/markets/{MARKET_A001}"] == 1
    assert transport.total == 4
    await adapter.aclose()


@pytest.mark.asyncio
async def test_further_books_cost_one_request_each_inside_the_ttl() -> None:
    """The fix, measured: the metadata is resolved once, the book always.

    Four `get_book` calls over the SAME market (both outcomes, twice
    each) issued 16 requests before T38 -- four full metadata
    resolutions of identical data. They now issue 4 + 3 = 7: the three
    metadata requests once, and `/book` on every single call, because
    the book is the thing the pass exists to read and is never cached.

    Asserting `/book == 4` alongside the total is the load-bearing half.
    A cache that also memoized the BOOK would make the total smaller
    still and would be catastrophically wrong -- every pass would score
    a stale book.
    """
    transport = CountingTransport()
    adapter = PolymarketAdapter(transport=transport)

    for outcome in ("Yes", "No", "Yes", "No"):
        await adapter.get_book(MARKET_A001, outcome)

    assert transport.counts["clob.polymarket.com/book"] == 4
    assert transport.counts["gamma-api.polymarket.com/markets"] == 1
    assert transport.counts["gamma-api.polymarket.com/events"] == 1
    assert transport.counts[f"clob.polymarket.com/markets/{MARKET_A001}"] == 1
    assert transport.total == 7
    await adapter.aclose()


@pytest.mark.asyncio
async def test_the_events_listing_is_fetched_once_across_different_markets() -> None:
    """`GET /events` is the single biggest waste, and is global, not per-market.

    It returns the WHOLE listing and is byte-identical no matter which
    market asked, yet every `get_market()` issued one -- 400 full
    listings in a 400-book pass. Two different markets are used here
    precisely because a per-market cache would still fetch it twice;
    only a listing-level memo makes this 1.
    """
    transport = CountingTransport()
    adapter = PolymarketAdapter(transport=transport)

    await adapter.get_book(MARKET_A001, "Yes")
    await adapter.get_book(MARKET_A002, "Yes")

    assert transport.counts["gamma-api.polymarket.com/events"] == 1
    # Per-MARKET data is still fetched per market -- the memo is keyed,
    # not global. Without this, a bug that returned market A's metadata
    # for market B would pass the assertion above.
    assert transport.counts["gamma-api.polymarket.com/markets"] == 2
    assert transport.counts[f"clob.polymarket.com/markets/{MARKET_A001}"] == 1
    assert transport.counts[f"clob.polymarket.com/markets/{MARKET_A002}"] == 1
    await adapter.aclose()


@pytest.mark.asyncio
async def test_concurrent_books_for_one_market_share_a_single_lookup() -> None:
    """Single-flight, not merely a cache -- the duplicates are CONCURRENT.

    `scan()` interleaves its fetch specs, so a market's YES and NO
    books are admitted within the same semaphore wave and run at the
    same time. A plain value cache does not help there: both miss the
    empty cache and both resolve the metadata. Only the per-key lock in
    `_AsyncTtlMemo` collapses them into one.

    Eight concurrent `get_book` calls over two markets: 8 `/book`
    requests (never shared), 1 `/events`, and exactly 1 metadata
    resolution per market. Without single-flight `gamma/markets` would
    be up to 8 here, not 2.
    """
    transport = CountingTransport()
    adapter = PolymarketAdapter(transport=transport)

    await asyncio.gather(
        *(
            adapter.get_book(market_id, outcome)
            for market_id in (MARKET_A001, MARKET_A002)
            for outcome in ("Yes", "No")
            for _ in range(2)
        )
    )

    assert transport.counts["clob.polymarket.com/book"] == 8
    assert transport.counts["gamma-api.polymarket.com/events"] == 1
    assert transport.counts["gamma-api.polymarket.com/markets"] == 2
    assert transport.total == 8 + 1 + 2 + 2
    await adapter.aclose()


@pytest.mark.asyncio
async def test_a_zero_ttl_disables_the_memo_entirely() -> None:
    """The memo has an off switch, and it actually switches it off.

    `settings.polymarket_market_cache_ttl_s = 0.0` must restore the
    original, uncached behaviour exactly -- four requests per book --
    so an operator who ever needs guaranteed-fresh market metadata has
    a lever that does not require a code change. This also pins the
    "before" number the other tests are measured against, inside the
    same suite rather than in a commit message.
    """
    transport = CountingTransport()
    adapter = PolymarketAdapter(transport=transport, market_cache_ttl_s=0.0)

    await adapter.get_book(MARKET_A001, "Yes")
    await adapter.get_book(MARKET_A001, "No")

    assert transport.total == 8
    assert transport.counts["gamma-api.polymarket.com/events"] == 2
    await adapter.aclose()


def test_the_memo_survives_the_adapter_outliving_an_event_loop() -> None:
    """A long-lived adapter really does see more than one event loop.

    `app.tasks.scanner` runs each beat under its own `asyncio.run`, and
    in `"paper"` mode `app.venues.registry.get_read_adapter` hands back
    the PROCESS-WIDE `PaperVenueAdapter` singleton -- so the same
    `PolymarketAdapter` is reused across loops. Without the loop guard
    in `_lock_for` the second beat raises `RuntimeError: ... is bound
    to a different event loop`.

    THREE conditions have to hold together for that, and this test
    reproduces all three deliberately, because any one of them missing
    makes the test pass for the wrong reason:

      1. The lock is CONTENDED in loop 1. Verified on CPython 3.12.7:
         `asyncio.Lock` does not bind to a loop at all on the
         uncontended path, so a lock used by one caller at a time is
         portable and proves nothing. `scan()` contends every market's
         lock for real -- its interleaved fetch order puts a market's
         YES and NO books in the same admission wave.
      2. The cache MISSES in loop 2. A hit returns before the lock is
         touched. The real code misses every beat: the default TTL is
         half `scan_interval_s`.
      3. The lock is contended AGAIN in loop 2.

    Deliberately NOT an `async def` test: it needs two separate
    `asyncio.run` calls, which is the very thing being reproduced.
    """
    memo: adapter_module._AsyncTtlMemo[int] = adapter_module._AsyncTtlMemo(60.0)

    async def slow(n: int) -> int:
        # Long enough that the second caller genuinely blocks on the
        # lock rather than finding it free -- condition (1)/(3).
        await asyncio.sleep(0.01)
        return n

    async def one_beat(n: int) -> list[int]:
        return list(
            await asyncio.gather(*(memo.get("m", lambda: slow(n)) for _ in range(2)))
        )

    assert asyncio.run(one_beat(1)) == [1, 1]  # one lookup shared by two callers
    memo._values.clear()  # TTL expiry between beats -- condition (2)
    assert asyncio.run(one_beat(2)) == [2, 2]

    # ...and the same property at the adapter level, where a cached
    # value DOES survive: the metadata is not re-fetched on the second
    # loop, only the book.
    transport = CountingTransport()
    adapter = PolymarketAdapter(transport=transport)
    asyncio.run(adapter.get_book(MARKET_A001, "Yes"))
    assert transport.total == 4
    asyncio.run(adapter.get_book(MARKET_A001, "No"))
    assert transport.total == 5
    assert transport.counts["gamma-api.polymarket.com/events"] == 1
    asyncio.run(adapter.aclose())


@pytest.mark.asyncio
async def test_the_memo_is_bounded_and_does_not_grow_without_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In paper mode the memo is process-lifetime, so it must have a cap.

    `make_paper_adapter` caches one `PaperVenueAdapter` per venue for
    the life of the process, and its inner read adapter goes with it --
    so a `market_id`-keyed memo would otherwise accumulate an entry for
    every market the process ever fetched a book for. The cap is
    exercised with a deliberately tiny value rather than by fetching
    4096 markets.

    The claim is BOUNDED, not exactly capped. Eviction runs inside
    `_store`, so between two evictions each table can gain one entry per
    store, and the lock the evicting caller is currently holding always
    survives the sweep. Both tables therefore settle within a small
    constant of `cap` -- and, which is the actual point, neither grows
    with the 20 distinct keys used here.
    """
    cap = 2
    monkeypatch.setattr(adapter_module, "_MEMO_MAX_ENTRIES", cap)
    memo: adapter_module._AsyncTtlMemo[int] = adapter_module._AsyncTtlMemo(60.0)

    async def make(n: int) -> int:
        return n

    for i in range(20):
        await memo.get(f"key-{i}", lambda i=i: make(i))  # type: ignore[misc]

    assert len(memo._values) <= cap
    assert len(memo._locks) <= 2 * cap + 1


@pytest.mark.asyncio
async def test_get_market_itself_is_not_memoized() -> None:
    """`get_market()` is a public read and keeps its freshness contract.

    The memo serves `get_book`'s token-id resolution only. The matcher
    and the links API call `get_market()` to decide whether two markets
    describe the same event, and handing them a minute-old answer is a
    behaviour change nobody asked for -- the request amplification was
    never their fault. Two `get_market()` calls therefore still issue
    two Gamma market lookups.
    """
    transport = CountingTransport()
    adapter = PolymarketAdapter(transport=transport)

    await adapter.get_market(MARKET_A001)
    await adapter.get_market(MARKET_A001)

    assert transport.counts["gamma-api.polymarket.com/markets"] == 2
    assert transport.counts[f"clob.polymarket.com/markets/{MARKET_A001}"] == 2
    await adapter.aclose()
