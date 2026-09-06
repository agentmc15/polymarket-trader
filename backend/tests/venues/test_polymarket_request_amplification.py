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

    The claim is BOUNDED, not exactly capped. Value eviction runs inside
    `_store` and lock eviction inside `_lock_for`, so between two sweeps
    each table can gain one entry per call, and the lock the sweeping
    caller is currently holding always survives. Both tables therefore
    settle within a small constant of `cap` -- and, which is the actual
    point, neither grows with the 20 distinct keys used here.
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
async def test_a_failing_factory_does_not_grow_the_lock_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T43 F3 -- the cap has to bound the table the FAILURES fill.

    `_MEMO_MAX_ENTRIES`' own comment says it exists so a paper-mode memo
    (process-wide singleton, process-lifetime tables) cannot accumulate
    an entry per market the process ever saw. Until T43 it bounded
    `_values` only: the lock sweep sat BELOW `_store`'s under-cap early
    return, so it ran only when a SUCCESSFUL store had pushed the value
    table over the cap. A factory that always raises never stores
    anything, so `_values` never grew, so the sweep never ran -- while
    `_lock_for` went on minting one `asyncio.Lock` per key on every miss.
    Measured against the shipped cap of 4096, 10,000 failing lookups left
    `_values` at 0 and `_locks` at 10,000.

    The failing factory is the whole point and is not contrived: `get`
    documents a raise as UNCACHED and retried by the next caller, and a
    venue outage makes every lookup in a pass one of these.

    `_values` is asserted at 0 as well, because it is what makes the
    assertion above non-vacuous: it proves the keys really did miss and
    really did reach `_lock_for`, rather than the loop having been
    short-circuited somewhere earlier.
    """
    cap = 4
    monkeypatch.setattr(adapter_module, "_MEMO_MAX_ENTRIES", cap)
    memo: adapter_module._AsyncTtlMemo[int] = adapter_module._AsyncTtlMemo(60.0)

    async def always_fails() -> int:
        raise RuntimeError("venue is down")

    for i in range(200):
        with pytest.raises(RuntimeError):
            await memo.get(f"key-{i}", always_fails)

    assert len(memo._values) == 0, "a raising factory must cache nothing"
    assert len(memo._locks) <= cap + 1, (
        f"200 failing lookups left {len(memo._locks)} locks behind a cap of {cap}"
    )


@pytest.mark.asyncio
async def test_both_tables_are_mutated_only_under_the_thread_guard() -> None:
    """T43 F4 -- the check-then-mutate is serialized, and stays serialized.

    `_lock_for` reads `_lock_loop`, then reads `_locks` -- on state
    reachable from a PROCESS-WIDE singleton, with nothing between the two
    halves. Two OS threads each inside their own `asyncio.run` can
    interleave there so that one is handed a lock the other created for
    a different loop (`RuntimeError: ... is bound to a different event
    loop`), or tear `_store`'s comprehensions apart mid-rebuild
    (`RuntimeError: dictionary changed size during iteration`).

    THE RACE ITSELF IS NOT REPRODUCED HERE, deliberately. It needs
    `sys.setswitchinterval(1e-7)` to surface -- at the default 5 ms,
    4 threads x 3000 rounds produce zero errors -- and at that interval
    the workload takes minutes, which is not a unit test, it is a flaky
    one. What IS deterministic, fast and exactly as load-bearing is the
    property the fix rests on: every MUTATION of the two tables happens
    inside `_guard`. Recording the guard's acquisitions around one `get`
    pins both call sites at once -- `_lock_for` before the factory runs,
    `_store` after -- so a later edit cannot quietly drop one.

    Why serialize rather than document: the shipped Celery prefork pool
    gives one `asyncio.run` per PROCESS, so there is no second loop over
    this object today. It appears the day anyone runs `--pool=threads`
    or `gevent`, or drives the paper singleton from a worker thread --
    a one-word config change nobody would connect to this file. The
    critical sections are a few dict operations with no `await` in them.
    """
    memo: adapter_module._AsyncTtlMemo[int] = adapter_module._AsyncTtlMemo(60.0)
    events: list[str] = []
    real_guard = memo._guard

    class RecordingGuard:
        """Delegates to the real lock, noting when it is entered."""

        def __enter__(self) -> bool:
            events.append("guard-enter")
            return real_guard.__enter__()

        def __exit__(self, *exc: object) -> None:
            events.append("guard-exit")
            real_guard.release()

    memo._guard = RecordingGuard()  # type: ignore[assignment]

    async def make() -> int:
        events.append("factory")
        return 7

    assert await memo.get("m", make) == 7

    # `_lock_for` guarded (before the factory), `_store` guarded (after).
    assert events == [
        "guard-enter",
        "guard-exit",
        "factory",
        "guard-enter",
        "guard-exit",
    ]


@pytest.mark.asyncio
async def test_get_market_refetches_its_market_legs_and_shares_the_events_memo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exactly which of `get_market()`'s three legs are memoized (T43).

    `_book_market_memo` serves `get_book`'s token-id resolution only, and
    `get_market()` never reads it: the matcher and the links API call
    `get_market()` to decide whether two markets describe the same event,
    and handing them a minute-old answer is a behaviour change nobody
    asked for -- the request amplification was never their fault. Both
    per-market legs are therefore re-issued on every call.

    But the THIRD leg, `GET gamma/events`, goes through
    `_event_id_by_market_id` and so through the shared `_events_memo` --
    so `get_market()` is PARTIALLY memoized, and the comment on
    `_book_market_memo` used to claim it "stays UNMEMOIZED". Asserting
    `/events == 1` alongside the two `== 2`s is what pins the corrected
    claim: one field of the returned `VenueMarket` (`event_id`) may be up
    to `ttl_s` stale, and no other.
    """
    transport = CountingTransport()
    adapter = PolymarketAdapter(transport=transport)

    await adapter.get_market(MARKET_A001)
    await adapter.get_market(MARKET_A001)

    assert transport.counts["gamma-api.polymarket.com/markets"] == 2
    assert transport.counts[f"clob.polymarket.com/markets/{MARKET_A001}"] == 2
    assert transport.counts["gamma-api.polymarket.com/events"] == 1, (
        "the events leg is served by the shared memo, not re-fetched"
    )

    # And with the memo disabled, the same two calls cost two listings --
    # which is what makes the assertion above a statement about the MEMO
    # rather than about `/events` happening to be called once.
    fresh_transport = CountingTransport()
    fresh = PolymarketAdapter(transport=fresh_transport, market_cache_ttl_s=0.0)
    await fresh.get_market(MARKET_A001)
    await fresh.get_market(MARKET_A001)
    assert fresh_transport.counts["gamma-api.polymarket.com/events"] == 2

    await adapter.aclose()
    await fresh.aclose()
