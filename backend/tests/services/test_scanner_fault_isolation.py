"""T38 F2/F5 -- one bad book must cost one book, and be COUNTED.

THE OUTAGE THIS FILE EXISTS FOR. `app.services.scanner.scan()`'s
docstring has always promised that a failure reading one book "is logged
and skipped rather than aborting the pass", and `_fetch_book`'s own
docstring warned that "an uncaught exception here would turn one 404 into
every book this pass was fetching, from every venue, being lost". Before
T38 that is exactly what a 404 did. `_fetch_book` caught `VenueError`
only, and the adapters DELIBERATELY do not flatten the common failures
into `VenueError`: `app.venues.kalshi.adapter.raise_for_venue_error` maps
429 and 401/403 and lets every other status fall through to
`httpx.Response.raise_for_status()` (its docstring says so), and
Polymarket's `get_book` calls a bare `raise_for_status()`. So a 404 on a
delisted ticker, a 500, or a read timeout arrived as `httpx.HTTPError`,
escaped the unit of work, and propagated out of an `asyncio.gather`
running with the default `return_exceptions=False`.

Operationally that is an indefinite outage, not a blip:
`app.tasks.scanner.scan_opportunities` is a 120-second beat, so ONE
reissued ticker answering 404 makes every pass raise, produce zero
opportunities, and never reach `session.commit()` -- with no signal
other than opportunities silently drying up.

WHAT IS DELIBERATELY *NOT* FIXED, and is tested here as such: a genuine
PROGRAMMING error still aborts the pass, loudly. Catching bare
`Exception` per book would report an `AttributeError` in a parsing path
as "the venue was flaky"; every book would take that same branch, so the
pass would return a handful of books and read as a quiet venue rather
than the defect it is. `app.services.scanner.VENUE_READ_FAULTS` is the
explicit line between the two, and both sides of it are proven below.

GUARDRAILS.md §1.4: no network to a venue, from any test, ever. Every
adapter here is `tests.venues.fixture_adapter.FixtureAdapter` or the
`FaultInjectingAdapter` subclass below, which raises pre-constructed
exception INSTANCES in process and performs no I/O of any kind -- the
`httpx` exceptions it raises are built by hand from an
`httpx.Request`/`httpx.Response` pair, never by making a request.
"""
import asyncio
import logging
from datetime import timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.models.event_link import EventLink
from app.models.intent import IntentRecord
from app.services.scanner import near_resolution_pass, scan
from app.utils.time import utcnow
from app.venues.base import VenueError
from app.venues.types import OrderBook, VenueId
from tests.helpers import make_book
from tests.venues.fixture_adapter import FixtureAdapter, make_venue_market

#: Long enough to clear `app.services.scoring`'s 200-char `rules_text`
#: penalty (same convention as `tests/services/test_scanner_concurrency.py`).
LONG_RULES_TEXT = "This market resolves according to the stated rules. " * 5

#: In-memory delay per book in the 800-book blast-radius tests. Chosen
#: so the whole fetch phase is ~40 waves x 2ms = ~80ms (fast), while
#: still being long enough that an ABANDONED sibling has demonstrably
#: not finished when `scan()` returns. Not a timing assertion: nothing
#: below asserts on elapsed time.
_FETCH_DELAY_S = 0.002

#: A hand-built request/response pair, so the `httpx` exceptions raised
#: below are the REAL exception types with the REAL status attached --
#: without any transport, socket, or venue being touched.
_FAKE_REQUEST = httpx.Request("GET", "https://clob.example.test/book")
_FAKE_404 = httpx.Response(404, request=_FAKE_REQUEST, json={"error": "not found"})


def a_404() -> httpx.HTTPStatusError:
    """The exception a delisted/reissued ticker produces on `GET /book`.

    Exactly what `httpx.Response.raise_for_status()` raises on a 404 --
    the call BOTH adapters make (Kalshi's `raise_for_venue_error` falls
    through to it for every status but 429/401/403; Polymarket's
    `get_book` calls it bare).
    """
    return httpx.HTTPStatusError(
        "Client error '404 Not Found'", request=_FAKE_REQUEST, response=_FAKE_404
    )


def a_timeout() -> httpx.ReadTimeout:
    """The commonest venue failure of all: the read timed out."""
    return httpx.ReadTimeout("timed out", request=_FAKE_REQUEST)


def a_transport_error() -> httpx.ConnectError:
    """A connection that never got established -- venue down, DNS, TLS."""
    return httpx.ConnectError("connection refused", request=_FAKE_REQUEST)


class FaultInjectingAdapter(FixtureAdapter):
    """`FixtureAdapter` that raises a chosen exception for ONE book.

    The point of raising for exactly one `(market_id, outcome)` -- rather
    than for a whole venue -- is that the pass's OTHER books are the
    assertion. A test that failed every book could not tell "one bad
    book cost one book" from "one bad book cost everything".

    Attributes:
        books_returned: How many `get_book` calls actually returned a
            book. This is the number that collapsed from 799 to 39
            before T38.
    """

    def __init__(
        self,
        venue: VenueId = "polymarket",
        *,
        fail_on: tuple[str, str] | None = None,
        error_factory: Any = None,
        delay_s: float = 0.0,
        **kwargs: Any,
    ) -> None:
        """Build the probe.

        Args:
            venue: `"polymarket"` or `"kalshi"`.
            fail_on: The single `(market_id, outcome)` that raises.
            error_factory: Zero-argument callable returning the exception
                INSTANCE to raise. A factory, not an instance, so each
                raise gets a fresh traceback-free exception.
            delay_s: An in-memory `asyncio.sleep` before each book
                returns. NOT for timing assertions -- it is what makes
                `books_returned` meaningful AT THE MOMENT `scan()`
                returns. With no delay every fetch completes in one
                scheduler pass, so even a pass that aborted would show a
                full count once its orphaned tasks drained, and the
                assertion would be vacuous. See
                `test_one_failing_book_costs_exactly_one_book`.
            **kwargs: Forwarded to `FixtureAdapter.__init__`.
        """
        super().__init__(venue, **kwargs)
        self._fail_on = fail_on
        self._error_factory = error_factory
        self._delay_s = delay_s
        self.books_returned = 0

    async def get_book(self, market_id: str, outcome: str) -> OrderBook:
        """Raise for the one designated book; otherwise delegate and count."""
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        if self._fail_on == (market_id, outcome) and self._error_factory is not None:
            raise self._error_factory()
        book = await super().get_book(market_id, outcome)
        self.books_returned += 1
        return book


@pytest_asyncio.fixture
async def sessions(test_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """An `async_sessionmaker` over the shared in-memory `test_engine`."""
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


def _add_markets(adapter: FixtureAdapter, venue: VenueId, count: int, prefix: str) -> None:
    """Register `count` open markets, each with a valid static YES/NO book."""
    now = utcnow()
    for i in range(count):
        market_id = f"{prefix}-{i}"
        adapter.add_market(
            make_venue_market(
                venue,
                market_id,
                close_time=now + timedelta(days=10),
                rules_text=LONG_RULES_TEXT,
                raw={"volume": float(1_000 - i)},
            )
        )
        for outcome in ("YES", "NO"):
            adapter.set_book(
                make_book(
                    bids=[(0.40, 100.0)],
                    asks=[(0.60, 100.0)],
                    venue=venue,
                    market_id=market_id,
                    outcome=outcome,
                )
            )


async def _run_one_failing_book(
    sessions: async_sessionmaker[AsyncSession], error_factory: Any
) -> tuple[FaultInjectingAdapter, FaultInjectingAdapter]:
    """Run a full-shape `scan()` in which exactly ONE book raises.

    The shape is the real one T38 was measured on: 200 markets per venue
    x 2 outcomes x 2 venues = 800 books, at the default concurrency
    bound of 20. Exactly one of those 800 raises, so the surviving count
    is a direct, non-vacuous readout of the blast radius -- 799 when the
    isolation works, 39 when it does not.

    `_FETCH_DELAY_S` is what makes that count mean anything. With no
    delay, all 800 fetches complete in a single scheduler pass, so even
    the broken code shows 799 by the time anything reads the counter --
    `asyncio.gather` does not CANCEL the siblings when one raises, it
    abandons them, and they finish in the background. The delay is what
    lets the count be read at the moment `scan()` actually returned.
    """
    pm = FaultInjectingAdapter(
        "polymarket",
        fail_on=("PM-ISO-7", "YES"),
        error_factory=error_factory,
        delay_s=_FETCH_DELAY_S,
    )
    kx = FaultInjectingAdapter("kalshi", delay_s=_FETCH_DELAY_S)
    _add_markets(pm, "polymarket", 200, "PM-ISO")
    _add_markets(kx, "kalshi", 200, "KX-ISO")

    async with sessions() as session:
        await scan((), {"polymarket": pm, "kalshi": kx}, [], session)
    return pm, kx


@pytest.mark.parametrize(
    ("name", "error_factory"),
    [
        ("a_404", a_404),
        ("a_read_timeout", a_timeout),
        ("a_transport_error", a_transport_error),
        # The one failure the pre-T38 code already handled -- kept in the
        # same parametrize so the three new cases are visibly held to the
        # SAME standard as the case that always worked, rather than to a
        # weaker one written to fit them.
        ("a_venue_error", lambda: VenueError("rate limited")),
    ],
)
async def test_one_failing_book_costs_exactly_one_book(
    sessions: async_sessionmaker[AsyncSession], name: str, error_factory: Any
) -> None:
    """800 books, one raises, 799 survive -- for every venue-fault shape.

    Measured against the pre-T38 source on this exact shape (200+200
    markets, bound 20, one book raising, read at the instant `scan()`
    returned):

        httpx.HTTPStatusError (a 404 on /book) -> scan() RAISED;   39/800
        httpx.ReadTimeout                      -> scan() RAISED;   39/800
        httpx.ConnectError                     -> scan() RAISED;   39/800
        VenueError (the one it did catch)      -> scan() survived; 799/800

    39 is not a tuning artifact: it is the two semaphore waves that had
    completed when the first raise reached the `gather`. The remaining
    ~760 fetches were NOT cancelled -- letting the event loop run on
    after the raise drains them to 799 -- which is WORSE than
    cancellation, not better: they keep holding the semaphore and
    issuing venue requests into a loop `app.tasks.scanner` is about to
    tear down with `asyncio.run`. `return_exceptions=True` is what
    removes the orphans as well as the outage.

    The `VenueError` row is the control. It is parametrized alongside
    the three new shapes so they are visibly held to the SAME standard
    as the case that always worked, rather than to one written to fit
    them.
    """
    pm, kx = await _run_one_failing_book(sessions, error_factory)

    assert pm.books_returned + kx.books_returned == 799
    # ...and specifically, the OTHER venue lost nothing at all. A
    # cross-venue pass that keeps 799 books but drops them all from one
    # venue would still be broken.
    assert kx.books_returned == 400
    assert pm.books_returned == 399


async def test_a_404_does_not_stop_the_pass_from_committing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The operational consequence: a 404 must not blank the beat's output.

    `scan_opportunities` runs every 120s and its ONLY output is the rows
    `scan()` commits. Before T38 a single reissued ticker answering 404
    made every pass raise before `session.commit()`, so the beat produced
    nothing, indefinitely, while looking merely quiet. This asserts on
    the committed ROWS, not on the return value, because the commit is
    the part that was being lost.

    The planted gap is the same one `tests/api/test_arbitrage.py` uses:
    YES ask 0.40 + NO ask 0.50 -> gross edge 10%, which clears
    `binary_complement_arbitrage`'s 2% floor by hand --
    yes_fee = 0.05 * 0.40 * 0.60 = 0.012,
    no_fee  = 0.05 * 0.50 * 0.50 = 0.0125,
    gas     = 2 * 0.05 / 100     = 0.001,
    net edge = 0.10 - 0.012 - 0.0125 - 0.001 = 0.0745 >= 0.02.
    """
    now = utcnow()
    pm = FaultInjectingAdapter(
        "polymarket", fail_on=("PM-DEAD", "YES"), error_factory=a_404
    )
    # The market whose book 404s -- a delisted/reissued ticker.
    pm.add_market(
        make_venue_market(
            "polymarket", "PM-DEAD", close_time=now + timedelta(days=10)
        )
    )
    pm.set_book(
        make_book(
            bids=[(0.30, 10.0)],
            asks=[(0.70, 10.0)],
            venue="polymarket",
            market_id="PM-DEAD",
            outcome="NO",
        )
    )
    # ...and a perfectly healthy market carrying a real opportunity.
    pm.add_market(
        make_venue_market(
            "polymarket",
            "PM-GAP",
            close_time=now + timedelta(days=10),
            rules_text=LONG_RULES_TEXT,
            resolution_source="Official Source",
            raw={"volume": 500_000.0},
        )
    )
    pm.set_book(
        make_book(
            bids=[(0.38, 200.0)],
            asks=[(0.40, 200.0)],
            venue="polymarket",
            market_id="PM-GAP",
            outcome="YES",
        )
    )
    pm.set_book(
        make_book(
            bids=[(0.48, 200.0)],
            asks=[(0.50, 200.0)],
            venue="polymarket",
            market_id="PM-GAP",
            outcome="NO",
        )
    )

    async with sessions() as session:
        scored = await scan(
            ["binary_complement_arbitrage"], {"polymarket": pm}, [], session
        )

    assert len(scored) >= 1
    # The rows are actually IN the database, i.e. `session.commit()` ran.
    async with sessions() as session:
        persisted = (await session.execute(select(IntentRecord))).scalars().all()
    assert [row.id for row in persisted] == [item.intent_record_id for item in scored]


async def test_a_programming_error_in_a_book_fetch_still_aborts_the_pass(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The other half of the decision: a BUG is not "the venue was flaky".

    `VENUE_READ_FAULTS` is deliberately not `Exception`. An
    `AttributeError` raised while parsing a book would be raised for
    EVERY book, so swallowing it per-book would turn a code defect into
    a pass that returns a handful of books, logs `debug` lines, and
    reads like a quiet venue. It must surface with its traceback
    instead. This is the assertion that stops a future "just catch
    everything" simplification from passing review.

    It aborts CLEANLY, though, which is the second assertion. Under the
    old `return_exceptions=False` the raise propagated the instant the
    bug fired, leaving every other in-flight fetch running detached --
    still holding the semaphore and still issuing venue requests into a
    loop `app.tasks.scanner` was about to tear down with `asyncio.run`.
    Collecting the results first means every sibling has settled before
    the error surfaces: 19 of 20 books are accounted for, not 3.
    """
    pm = FaultInjectingAdapter(
        "polymarket",
        fail_on=("PM-BUG-3", "NO"),
        error_factory=lambda: AttributeError("'NoneType' object has no attribute 'get'"),
        delay_s=_FETCH_DELAY_S,
    )
    _add_markets(pm, "polymarket", 10, "PM-BUG")

    async with sessions() as session:
        with pytest.raises(AttributeError):
            await scan((), {"polymarket": pm}, [], session)

    # 10 markets x 2 outcomes = 20 books; one was the bug. Every other
    # fetch finished BEFORE the error propagated -- nothing was left
    # orphaned behind the abort.
    assert pm.books_returned == 19


async def test_a_failed_book_is_counted_and_logged_not_silently_absent(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    """A pass that quietly returns 39 of 800 books is its own failure mode.

    Surviving the 404 is only half the fix: the failure has to be
    VISIBLE. The per-book line stays at `debug` on purpose (an
    unreachable venue would otherwise emit 800 warnings), so the
    one-per-pass `scan_book_fetch_complete` record is the operator's
    signal -- and it must be a WARNING when anything failed, carry a
    non-zero `books_failed`, and attribute it to the venue and the
    error type. A record saying `books_requested=40, books_fetched=39`
    with no failure count is exactly the "silently absent" shape this
    asserts against.
    """
    pm = FaultInjectingAdapter(
        "polymarket", fail_on=("PM-LOG-2", "YES"), error_factory=a_404
    )
    kx = FaultInjectingAdapter("kalshi")
    _add_markets(pm, "polymarket", 10, "PM-LOG")
    _add_markets(kx, "kalshi", 10, "KX-LOG")

    with caplog.at_level(logging.INFO, logger="app.services.scanner"):
        async with sessions() as session:
            await scan((), {"polymarket": pm, "kalshi": kx}, [], session)

    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "scan_book_fetch_complete"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.books_requested == 40  # type: ignore[attr-defined]
    assert record.books_fetched == 39  # type: ignore[attr-defined]
    assert record.books_failed == 1  # type: ignore[attr-defined]
    assert record.book_failures_by_venue == {"polymarket": 1}  # type: ignore[attr-defined]
    assert record.book_failures_by_error == {"HTTPStatusError": 1}  # type: ignore[attr-defined]


async def test_a_clean_pass_reports_zero_failures_at_info(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    """The counter is a real count, not a constant: no failure, no warning.

    Without this, `test_a_failed_book_is_counted_and_logged...` could be
    satisfied by hard-coding `books_failed=1` and WARNING. Pairing the
    two is what makes either mean anything.
    """
    pm = FaultInjectingAdapter("polymarket")
    _add_markets(pm, "polymarket", 5, "PM-CLEAN")

    with caplog.at_level(logging.INFO, logger="app.services.scanner"):
        async with sessions() as session:
            await scan((), {"polymarket": pm}, [], session)

    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "scan_book_fetch_complete"
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert records[0].books_failed == 0  # type: ignore[attr-defined]
    assert records[0].books_fetched == 10  # type: ignore[attr-defined]


async def test_scan_reports_the_pair_skew_it_actually_achieved(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    """T38 F5: the per-link leg-to-leg skew is MEASURED, not asserted.

    `_interleave_fetch_specs` improves the MEAN pair skew and does not
    bound the worst case (34.1% vs 51.2% of the window for 200-vs-200
    markets, 47.8% vs 52.5% for 200-vs-20, 100% worst case under both
    orders), so the scanner reports the number an operator actually
    needs -- the gap between when a link's two legs' books landed --
    rather than claiming a bound ordering cannot provide.

    Two APPROVED links whose legs are all fetched, one PROPOSED link
    that must be ignored (PLAN.md D9: nothing trades on a proposed
    link, so nothing about it belongs in this health metric either).
    `links_skew_measured == 2` is what proves the filter and the
    measurement both ran -- a "0 of 0 links measured" report would be
    vacuous.
    """
    pm = FaultInjectingAdapter("polymarket")
    kx = FaultInjectingAdapter("kalshi")
    _add_markets(pm, "polymarket", 5, "PM-SKEW")
    _add_markets(kx, "kalshi", 5, "KX-SKEW")

    links = [
        _link(1, "PM-SKEW-0", "KX-SKEW-0", status="approved"),
        _link(2, "PM-SKEW-1", "KX-SKEW-1", status="approved"),
        _link(3, "PM-SKEW-2", "KX-SKEW-2", status="proposed"),
    ]

    with caplog.at_level(logging.INFO, logger="app.services.scanner"):
        async with sessions() as session:
            await scan((), {"polymarket": pm, "kalshi": kx}, links, session)

    record = next(
        r for r in caplog.records
        if getattr(r, "event", None) == "scan_book_fetch_complete"
    )
    assert record.links_skew_measured == 2  # type: ignore[attr-defined]
    # A real elapsed measurement: non-negative, and no leg pair can be
    # further apart than the whole fetch window it was measured inside.
    assert record.max_link_pair_skew_s >= 0.0  # type: ignore[attr-defined]
    assert record.mean_link_pair_skew_s <= record.max_link_pair_skew_s  # type: ignore[attr-defined]
    assert record.max_link_pair_skew_s <= record.fetch_elapsed_s  # type: ignore[attr-defined]


async def test_pair_skew_ignores_a_link_whose_leg_never_landed(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    """An unmeasurable pair must not be averaged in as a zero.

    If one leg's book failed, the pass has NO reading for that link's
    skew. Counting it as `0.0` would make a partially-broken venue look
    like the freshest possible snapshot -- the exact inversion of what
    the metric is for. The link is dropped from the measurement instead,
    which the reported count makes visible.
    """
    pm = FaultInjectingAdapter(
        "polymarket", fail_on=("PM-GONE-0", "YES"), error_factory=a_timeout
    )
    kx = FaultInjectingAdapter("kalshi")
    _add_markets(pm, "polymarket", 3, "PM-GONE")
    _add_markets(kx, "kalshi", 3, "KX-GONE")

    links = [
        # Both legs land -> measured.
        _link(1, "PM-GONE-1", "KX-GONE-1", status="approved"),
        # One leg names a market this pass produced no book for at all,
        # so there is no timestamp to difference -> not measured.
        # (Deliberately NOT `PM-GONE-0`, whose YES book timed out: its
        # NO book still landed, so that market DOES have a completion
        # time and its link is still measurable. The metric drops a leg
        # with no reading whatsoever, not a leg that lost one book.)
        _link(2, "PM-NEVER", "KX-GONE-2", status="approved"),
    ]

    with caplog.at_level(logging.INFO, logger="app.services.scanner"):
        async with sessions() as session:
            await scan((), {"polymarket": pm, "kalshi": kx}, links, session)

    record = next(
        r for r in caplog.records
        if getattr(r, "event", None) == "scan_book_fetch_complete"
    )
    assert record.links_skew_measured == 1  # type: ignore[attr-defined]


def _link(link_id: int, market_a: str, market_b: str, *, status: str) -> EventLink:
    """Build an unsaved `EventLink` with an explicit id and status.

    `status` is set explicitly because `"proposed"` is a COLUMN default
    applied at INSERT -- an unsaved row's `status` is otherwise `None`,
    which would silently pass an `== "approved"` filter test for the
    wrong reason.
    """
    link = EventLink(
        venue_a="polymarket",
        market_a=market_a,
        venue_b="kalshi",
        market_b=market_b,
        outcome_map={"YES": "YES", "NO": "NO"},
        confidence=0.9,
        evidence={"title_jaccard": 1.0},
        status=status,
    )
    link.id = link_id
    return link


@pytest.mark.parametrize(
    ("name", "error_factory"),
    [("a_404", a_404), ("a_read_timeout", a_timeout), ("a_transport_error", a_transport_error)],
)
async def test_near_resolution_pass_survives_one_failing_book(
    sessions: async_sessionmaker[AsyncSession], name: str, error_factory: Any
) -> None:
    """The same outage lived next door, in the serial near-resolution pass.

    `near_resolution_pass` has no `asyncio.gather` to cancel, but its
    `get_book` catch was the identical too-narrow `except VenueError`:
    one 404 propagated out of the whole function, losing every market it
    had not reached yet AND the `session.commit()` at the end. Proven on
    the same three fault shapes, by asserting the LATER markets' books
    were still read.
    """
    now = utcnow()
    pm = FaultInjectingAdapter(
        "polymarket", fail_on=("PM-NR-0", "YES"), error_factory=error_factory
    )
    for i in range(5):
        market_id = f"PM-NR-{i}"
        pm.add_market(
            make_venue_market(
                "polymarket",
                market_id,
                close_time=now + timedelta(hours=1),
                rules_text=LONG_RULES_TEXT,
            )
        )
        for outcome in ("YES", "NO"):
            pm.set_book(
                make_book(
                    bids=[(0.40, 100.0)],
                    asks=[(0.60, 100.0)],
                    venue="polymarket",
                    market_id=market_id,
                    outcome=outcome,
                )
            )

    settings_obj = Settings(near_resolution_hours=24.0)
    async with sessions() as session:
        await near_resolution_pass(
            {"polymarket": pm}, session, settings_obj=settings_obj
        )

    # 5 markets x 2 outcomes = 10 books; exactly one raised. Before the
    # fix the pass aborted on market 0, so markets 1-4 were never read
    # at all and this would be 1, not 9.
    assert pm.books_returned == 9
