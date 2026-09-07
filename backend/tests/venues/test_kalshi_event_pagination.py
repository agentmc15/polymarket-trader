"""Kalshi's event listing must be walked to the end, not to page 10.

MEASURED AGAINST THE LIVE API. `/events?status=open&with_nested_markets=
true` exhausts after 57 pages and yields **99,253** open markets in 10.1
seconds. The adapter stopped at `_MAX_EVENT_PAGES = 10` and saw
**14,028** — 14% of the venue.

THE TRUNCATION IS NOT RANDOM, WHICH IS THE WHOLE PROBLEM. Kalshi returns
its near-dated markets in the TAIL. Counting by close time across all 57
pages:

    5,106 markets close within 24 hours
   16,143 within 7 days
   37,554 within 30 days

Of those 37,554, exactly **27** fell inside the ten-page cap. The
sub-24-hour markets — crypto prices, hourly index levels, same-day game
lines, the most actively quoted contracts on the venue — were absent
entirely. 27,063 of the near-dated markets carry a live bid, so this is
not a tail of dead contracts.

WHY THE OLD REASONING FAILED. The constant's comment argued that "10
pages is far more than `scan_top_n` (200) can consume". That is true only
if the listing arrives ordered by the thing being selected on, and it
does not: truncating a listing that is not sorted by volume does not
yield the top 200 by volume, it yields an arbitrary 14% of the venue that
happens to be page-ordered. Worse, `scan_near_resolution` selects on
CLOSE TIME, not volume — so the one beat whose entire purpose is
"events culminating soon" was reading the slice of the venue that
systematically excluded them, and reporting an empty bucket as a fact
about the market.

`GET /markets` is not the alternative: filtered by `max_close_ts` to the
next 30 days it needs 17.5s to return 80,000 rows of which 5% carry a
live bid, against 10.1s for the complete 99,253 here.

The cap stays — an endpoint handing back a fresh cursor forever must not
spin this coroutine — but it is set with real headroom over the observed
57, and reaching it now says so out loud instead of silently returning a
short listing.
"""
import logging

import pytest

from app.venues.kalshi.adapter import (
    _EVENTS_PAGE_LIMIT,
    _MAX_EVENT_PAGES,
    KalshiAdapter,
)


def _event(index: int) -> dict:
    return {
        "event_ticker": f"EV{index}",
        "markets": [{
            "ticker": f"EV{index}-M",
            "title": f"Question {index}?",
            "close_time": "2026-12-31T23:59:00Z",
            "status": "active",
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.42",
        }],
    }


class _Pager:
    """Serves `pages` pages of events, then stops handing back a cursor."""

    def __init__(self, pages: int) -> None:
        self.pages = pages
        self.calls = 0

    async def get(self, path: str, params: dict | None = None) -> dict:  # noqa: ARG002
        assert path == "/events"
        self.calls += 1
        last = self.calls >= self.pages
        base = self.calls * 1000
        return {
            "events": [_event(base + i) for i in range(3)],
            "cursor": "" if last else f"cur{self.calls}",
        }


@pytest.mark.asyncio
async def test_the_listing_is_walked_past_the_old_ten_page_limit() -> None:
    """57 pages live; ten was not "far more" than anything."""
    adapter = KalshiAdapter()
    pager = _Pager(pages=57)
    adapter._get = pager.get  # type: ignore[method-assign]

    payloads = await adapter._fetch_open_event_markets()

    assert pager.calls == 57, f"stopped after {pager.calls} pages"
    assert len(payloads) == 57 * 3


@pytest.mark.asyncio
async def test_the_cap_has_headroom_over_what_the_venue_actually_serves() -> None:
    """A regression pin on the constant itself.

    The live listing needed 57 pages on the day this was measured, and a
    venue only ever lists more contracts. If this is ever tuned back down
    near that number, the near-dated tail starts disappearing again — and
    it disappears silently, as a smaller opportunity count.
    """
    assert _MAX_EVENT_PAGES >= 100
    assert _EVENTS_PAGE_LIMIT == 200


@pytest.mark.asyncio
async def test_reaching_the_cap_is_logged_rather_than_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The old loop just returned; a short listing reads as a small venue."""
    adapter = KalshiAdapter()
    # Never stops offering a fresh cursor: the pathological case the cap
    # exists to bound.
    pager = _Pager(pages=_MAX_EVENT_PAGES + 50)
    adapter._get = pager.get  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING):
        payloads = await adapter._fetch_open_event_markets()

    assert pager.calls == _MAX_EVENT_PAGES
    assert len(payloads) == _MAX_EVENT_PAGES * 3
    assert any(
        record.__dict__.get("event") == "kalshi_event_page_cap_reached"
        for record in caplog.records
    ), "hitting the cap must say so"


@pytest.mark.asyncio
async def test_a_repeated_cursor_still_ends_the_walk() -> None:
    """The existing loop guard must survive the raised cap."""
    adapter = KalshiAdapter()

    calls = {"n": 0}

    async def stuck(path: str, params: dict | None = None) -> dict:  # noqa: ARG001
        calls["n"] += 1
        return {"events": [_event(calls["n"])], "cursor": "same"}

    adapter._get = stuck  # type: ignore[method-assign]

    payloads = await adapter._fetch_open_event_markets()

    assert calls["n"] == 2, "a repeated cursor must stop the walk immediately"
    assert len(payloads) == 2
