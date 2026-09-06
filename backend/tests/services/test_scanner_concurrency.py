"""T35 -- `app.services.scanner.scan()`'s concurrent, bounded book fetch.

Before T35, `scan()` awaited one `get_book` call at a time: up to
`scan_top_n` (200) x 2 outcomes x 2 venues = 800 sequential HTTP round
trips a pass. Two problems, not one: (1) rate-limit risk, now shared by
THREE beats (`scan_opportunities`, `scan_near_resolution`,
`propose_event_links`), and (2) SNAPSHOT SKEW -- a cross-venue arbitrage
signal claims two prices are inconsistent AT THE SAME MOMENT, and a
multi-minute serial walk compares books read minutes apart, which can
manufacture an "edge" that is purely a clock artifact. This module proves
the fix does what it claims: books are fetched CONCURRENTLY (not merely
reordered -- a test that only compared output would pass unchanged
against the old serial code), the concurrency bound
(`settings.scan_book_fetch_concurrency`) is actually respected, TWO
different venues' books are interleaved in time rather than one venue's
whole batch completing before the other's begins, and a venue whose
every book fetch fails does not abort the pass or lose the OTHER venue's
opportunities.

GUARDRAILS.md §1.4: no network to a venue, from any test, ever. Every
adapter here is `tests.venues.fixture_adapter.FixtureAdapter` (or the
`ConcurrencyProbeAdapter` subclass below it, which adds only an
in-memory `asyncio.sleep` delay and in-process counters -- no I/O of any
kind). GUARDRAILS.md §5: every timing test states, in a comment, both
the serial floor and the concurrent ceiling the constants below imply,
so the threshold asserted is visibly derived, not picked to make the
test pass.
"""
import asyncio
import time
from datetime import timedelta
from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.services.scanner import scan
from app.utils.time import utcnow
from app.venues.base import VenueError
from app.venues.types import OrderBook, VenueId
from tests.helpers import make_book
from tests.venues.fixture_adapter import FixtureAdapter, make_venue_market

#: Long enough to clear `app.services.scoring`'s 200-char `rules_text`
#: penalty (same convention as `tests/api/test_arbitrage.py` and
#: `tests/services/test_near_resolution.py`).
LONG_RULES_TEXT = "This market resolves according to the stated rules. " * 5


@pytest_asyncio.fixture
async def sessions(test_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """An `async_sessionmaker` over the shared in-memory `test_engine`."""
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


class _SharedInFlight:
    """Mutable in-flight/max-in-flight counters, shared across adapters.

    One `FixtureAdapter` instance only sees its OWN `get_book` calls.
    Sharing one of these across two adapter instances (one per venue) is
    what lets a test observe the concurrency bound as `scan()` actually
    applies it -- GLOBALLY, across every venue together, not per venue
    (see `app.services.scanner`'s "BOOK FETCH IS CONCURRENT..." module
    docstring section for why a global bound is the point).
    """

    def __init__(self) -> None:
        self.in_flight = 0
        self.max_in_flight = 0

    def enter(self) -> None:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)

    def exit(self) -> None:
        self.in_flight -= 1


class ConcurrencyProbeAdapter(FixtureAdapter):
    """`FixtureAdapter` that delays/fails `get_book` on demand and records
    concurrency and completion timing.

    A test can therefore PROVE `scan()`'s fetch phase is genuinely
    concurrent (elapsed wall time far below what serial execution could
    achieve, plus `counter.max_in_flight > 1`), that the concurrency
    bound is respected (`counter.max_in_flight <= bound`), and that two
    venues' books are interleaved in time (`completion_log` timestamps),
    rather than merely asserting the same opportunities a serial
    implementation would also produce.

    Attributes:
        counter: Shared `_SharedInFlight` (pass the SAME instance to
            more than one adapter to observe a bound across venues).
        completion_log: If given, `time.monotonic()` is appended the
            instant a successful `get_book` call finishes (after its
            artificial delay), so a test can compare WHEN different
            venues' books actually landed.
    """

    def __init__(
        self,
        venue: VenueId = "polymarket",
        *,
        delay_s: float = 0.0,
        fail_outcomes: frozenset[tuple[str, str]] = frozenset(),
        counter: "_SharedInFlight | None" = None,
        completion_log: list[float] | None = None,
        **kwargs: Any,
    ) -> None:
        """Build the probe over the same fixtures `FixtureAdapter` takes.

        Args:
            venue: `"polymarket"` or `"kalshi"`.
            delay_s: Artificial `asyncio.sleep` before every `get_book`
                returns -- long enough, relative to a test's assertions,
                to force real overlap between concurrent calls.
            fail_outcomes: `(market_id, outcome)` pairs that raise
                `VenueError` instead of returning a book, simulating an
                unreachable venue for exactly those calls.
            counter: Shared in-flight counter. Defaults to a private one
                if not given.
            completion_log: Optional shared list to append completion
                timestamps to.
            **kwargs: Forwarded to `FixtureAdapter.__init__`.
        """
        super().__init__(venue, **kwargs)
        self._delay_s = delay_s
        self._fail_outcomes = fail_outcomes
        self.counter = counter if counter is not None else _SharedInFlight()
        self.completion_log = completion_log

    async def get_book(self, market_id: str, outcome: str) -> OrderBook:
        """Delay, optionally fail, then delegate to `FixtureAdapter.get_book`."""
        self.counter.enter()
        try:
            if self._delay_s:
                await asyncio.sleep(self._delay_s)
            if (market_id, outcome) in self._fail_outcomes:
                raise VenueError("probe-induced failure")
            book = await super().get_book(market_id, outcome)
            if self.completion_log is not None:
                self.completion_log.append(time.monotonic())
            return book
        finally:
            self.counter.exit()


def _add_markets(adapter: FixtureAdapter, venue: VenueId, count: int, prefix: str) -> None:
    """Register `count` open markets, each with a valid static YES/NO book.

    Used only by the pure fetch-phase tests below, which run `scan()`
    with `strategy_names=()` -- no strategy runs, so the book PRICES are
    irrelevant; only that a book exists for every outcome, so every
    `get_book` call this pass issues succeeds (unless deliberately made
    to fail via `fail_outcomes`).
    """
    for i in range(count):
        market_id = f"{prefix}-{i}"
        adapter.add_market(make_venue_market(venue, market_id))
        for outcome in ("YES", "NO"):
            adapter.set_book(
                make_book(
                    bids=[(0.40, 100.0)], asks=[(0.60, 100.0)],
                    venue=venue, market_id=market_id, outcome=outcome,
                )
            )


async def test_scan_fetches_books_concurrently_not_serially(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Bounded-concurrent fetch finishes in far less than the serial floor.

    15 markets x 2 outcomes = 30 `get_book` calls, each artificially
    delayed 0.05s, bound at 5 concurrent. A SERIAL implementation (the
    code before T35) needs AT LEAST `30 * 0.05 = 1.5s` no matter what --
    every call is awaited one at a time. A bounded-concurrent
    implementation needs roughly `ceil(30 / 5) * 0.05 = 0.3s` (6
    admission waves). Asserting elapsed time is under `0.9s` (60% of the
    serial floor, comfortably above the concurrent ceiling to absorb CI
    jitter) is a threshold ONLY the concurrent implementation can meet --
    a test that instead compared `scan()`'s RESULTS would pass unchanged
    against the reverted, serial code and prove nothing about how the
    fetch was performed.
    """
    num_markets = 15
    delay_s = 0.05
    bound = 5
    counter = _SharedInFlight()
    adapter = ConcurrencyProbeAdapter("polymarket", delay_s=delay_s, counter=counter)
    _add_markets(adapter, "polymarket", num_markets, "PM-CONC")
    settings_obj = Settings(scan_book_fetch_concurrency=bound)

    async with sessions() as session:
        started = time.monotonic()
        await scan((), {"polymarket": adapter}, [], session, settings_obj=settings_obj)
        elapsed = time.monotonic() - started

    serial_floor_s = num_markets * 2 * delay_s  # 30 * 0.05 = 1.5s
    assert elapsed < serial_floor_s * 0.6  # 0.9s: below the serial floor
    # Real overlap actually happened, not just "somehow fast".
    assert counter.max_in_flight > 1


async def test_scan_book_fetch_respects_the_concurrency_bound(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """`counter.max_in_flight` never exceeds `scan_book_fetch_concurrency`,
    and the bound applies GLOBALLY (shared across both venues via one
    `counter`), not per venue.

    20 markets/venue x 2 outcomes x 2 venues = 80 total `get_book` calls,
    bound at 4 -- far more calls than the bound, so an
    `asyncio.Semaphore`-bounded implementation deterministically drives
    `max_in_flight` up to EXACTLY the bound (each of the first 4 tasks
    acquires the semaphore without suspending and then suspends on its
    own `asyncio.sleep`, so all 4 are genuinely concurrent before any
    releases) and never beyond it. A reverted, serial implementation
    would report `max_in_flight == 1` here, failing this assertion
    outright -- and a buggy per-venue-bound implementation (one
    semaphore per venue instead of one shared across both) would let
    this SAME shared counter see up to `2 * bound = 8` at once, also
    failing it.
    """
    num_markets = 20
    delay_s = 0.02
    bound = 4
    counter = _SharedInFlight()
    pm = ConcurrencyProbeAdapter("polymarket", delay_s=delay_s, counter=counter)
    kx = ConcurrencyProbeAdapter("kalshi", delay_s=delay_s, counter=counter)
    _add_markets(pm, "polymarket", num_markets, "PM-BOUND")
    _add_markets(kx, "kalshi", num_markets, "KX-BOUND")
    settings_obj = Settings(scan_book_fetch_concurrency=bound)

    async with sessions() as session:
        await scan(
            (), {"polymarket": pm, "kalshi": kx}, [], session, settings_obj=settings_obj
        )

    assert counter.max_in_flight == bound


async def test_scan_interleaves_both_venues_books_instead_of_one_then_the_other(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The actual defect T35 closes: TWO venues' books must land close
    together in time, not "venue A's whole batch, then venue B's".

    A single global semaphore is NOT enough on its own to guarantee
    this: `asyncio.Semaphore` queues blocked waiters FIFO in the order
    they first tried to acquire, so if the fetch specs were built
    venue-by-venue (every one of venue A's specs before venue B's
    first), venue B's earliest book could not even START until roughly
    `len(venue A's specs) / bound` admission waves had already drained
    -- reproducing "fetch A fully, then B" one layer down, which is
    exactly the clock-artifact risk this task exists to remove (a
    cross-venue signal compares two DIFFERENT venues' books). Round-
    robining the fetch specs across venues (`_interleave_fetch_specs`)
    is what prevents that.

    20 markets/venue x 2 outcomes = 40 calls/venue, 80 total, bound 4,
    delay 0.02s each. Venue-grouped order would push kalshi's first
    completion to roughly `40 / 4 * 0.02 = 0.2s` in (it cannot even
    begin until polymarket's 40 specs have mostly drained). Interleaved,
    kalshi's specs sit among the FIRST few admitted, so its earliest
    completion should land within roughly one or two 0.02s waves of the
    very start -- asserting `< 0.08s` (4 waves) is generous headroom
    above the ~0.02-0.04s interleaved case while sitting far below the
    ~0.2s venue-grouped case, so this assertion discriminates cleanly
    between the two fetch orders.
    """
    num_markets = 20
    delay_s = 0.02
    bound = 4
    counter = _SharedInFlight()
    pm_times: list[float] = []
    kx_times: list[float] = []
    pm = ConcurrencyProbeAdapter(
        "polymarket", delay_s=delay_s, counter=counter, completion_log=pm_times
    )
    kx = ConcurrencyProbeAdapter(
        "kalshi", delay_s=delay_s, counter=counter, completion_log=kx_times
    )
    _add_markets(pm, "polymarket", num_markets, "PM-SKEW")
    _add_markets(kx, "kalshi", num_markets, "KX-SKEW")
    settings_obj = Settings(scan_book_fetch_concurrency=bound)

    started = time.monotonic()
    async with sessions() as session:
        await scan(
            (), {"polymarket": pm, "kalshi": kx}, [], session, settings_obj=settings_obj
        )

    assert pm_times and kx_times
    kx_first = min(kx_times) - started
    pm_last = max(pm_times) - started
    # The two venues' read windows overlap almost entirely -- kalshi's
    # first book lands well before polymarket's last, not after it.
    assert kx_first < pm_last
    assert kx_first < delay_s * 4  # < 0.08s: see docstring's derivation


async def test_scan_survives_a_venue_whose_every_book_fetch_fails(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """One venue's `get_book` raises `VenueError` for every outcome; the
    OTHER venue's planted complement gap still surfaces, and `scan()`
    does not raise.

    Same planted gap as `tests/api/test_arbitrage.py::_planted_gap_adapter`
    (YES ask 0.40 + NO ask 0.50 -> gross edge 10%, clears
    `binary_complement_arbitrage`'s 2% floor by hand: yes_fee =
    0.05*0.40*0.60 = 0.012, no_fee = 0.05*0.50*0.50 = 0.0125, gas =
    2*0.05/100 = 0.001, net edge = 0.10 - 0.012 - 0.0125 - 0.001 =
    0.0745 >= 0.02).

    This is the concurrency-era version of the "a broken venue must not
    blank out the other's opportunities" guarantee `scan()`'s own
    docstring has always claimed -- proven here specifically against the
    concurrent fetch path (`_fetch_book` catching `VenueError` INSIDE
    each unit of work, not around the whole `asyncio.gather`), since
    that is exactly the seam this task touched.
    """
    now = utcnow()
    healthy = FixtureAdapter("polymarket")
    healthy.add_market(
        make_venue_market(
            "polymarket",
            "PM-GAP",
            close_time=now + timedelta(days=10),
            rules_text=LONG_RULES_TEXT,
            resolution_source="Official Source",
            raw={"volume": 500_000.0},
        )
    )
    healthy.set_book(
        make_book(
            bids=[(0.38, 200.0)], asks=[(0.40, 200.0)],
            venue="polymarket", market_id="PM-GAP", outcome="YES",
        )
    )
    healthy.set_book(
        make_book(
            bids=[(0.48, 200.0)], asks=[(0.50, 200.0)],
            venue="polymarket", market_id="PM-GAP", outcome="NO",
        )
    )

    counter = _SharedInFlight()
    failing = ConcurrencyProbeAdapter(
        "kalshi",
        counter=counter,
        fail_outcomes=frozenset({("KX-DOWN-0", "YES"), ("KX-DOWN-0", "NO")}),
    )
    failing.add_market(
        make_venue_market("kalshi", "KX-DOWN-0", close_time=now + timedelta(days=10))
    )
    # Deliberately NO books registered for KX-DOWN-0 -- every `get_book`
    # call for it must raise via `fail_outcomes` before ever touching
    # `FixtureAdapter`'s own `KeyError`-on-missing-book path.

    async with sessions() as session:
        scored = await scan(
            ["binary_complement_arbitrage"],
            {"polymarket": healthy, "kalshi": failing},
            [],
            session,
        )

    assert len(scored) >= 1
    assert all(leg.venue == "polymarket" for item in scored for leg in item.intent.legs)
    # Kalshi's book fetch was actually attempted (and failed), not
    # silently skipped -- proving the isolation is "caught", not "never
    # tried".
    assert counter.max_in_flight >= 1
