"""`KalshiAdapter._pace` bounds the aggregate request rate.

Kalshi's unauthenticated limit was measured at roughly 10 requests a
second: 17 succeeded in 1.69s and the 18th returned 429 with no
`Retry-After`. `list_markets` pages at `limit=200` for up to 50 pages as
fast as the network allows, so it tripped the limit on EVERY scan and
raised -- discarding the ~3400 markets it had already fetched. Kalshi
therefore contributed nothing to any pass, and cross-venue arbitrage,
the reason for scanning two venues at all, could never fire.

`_pace` is exercised directly here because the request path skips it
when a transport is injected: a `MockTransport` has no rate limit, and
pacing it only made the venue suite three times slower.
"""
import asyncio
import time

from app.config import Settings
from app.venues.kalshi.adapter import KalshiAdapter


def _adapter(interval: float) -> KalshiAdapter:
    return KalshiAdapter(
        settings_obj=Settings(KALSHI_MIN_REQUEST_INTERVAL_S=interval)
    )


def test_consecutive_calls_are_separated_by_the_interval() -> None:
    adapter = _adapter(0.05)

    async def run() -> float:
        t0 = time.perf_counter()
        for _ in range(4):
            await adapter._pace()
        return time.perf_counter() - t0

    # 4 calls => 3 enforced gaps.
    assert asyncio.run(run()) >= 0.05 * 3


def test_concurrent_callers_share_one_budget() -> None:
    """The limit is per client, not per call site.

    Book fetches run concurrently with the paging loop and draw on the
    same allowance, so pacing each coroutine independently would not
    bound the aggregate rate.
    """
    adapter = _adapter(0.05)

    async def run() -> float:
        t0 = time.perf_counter()
        await asyncio.gather(*(adapter._pace() for _ in range(4)))
        return time.perf_counter() - t0

    assert asyncio.run(run()) >= 0.05 * 3


def test_a_zero_interval_disables_pacing() -> None:
    adapter = _adapter(0.0)

    async def run() -> float:
        t0 = time.perf_counter()
        for _ in range(20):
            await adapter._pace()
        return time.perf_counter() - t0

    assert asyncio.run(run()) < 0.05
