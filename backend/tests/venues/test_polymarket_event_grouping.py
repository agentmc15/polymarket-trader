"""Polymarket markets must actually carry their `event_id`.

MEASURED ON LIVE DATA: `event_id` was `None` on 1,918 of 1,918 open
markets, while the vendor payload carried event grouping for every one of
them. Two independent defects, either sufficient:

1. KEY-SPACE MISMATCH. `_build_event_id_by_market_id` keys the mapping by
   the nested market's `id` — Gamma's numeric surrogate, e.g. `"239826"` —
   but `VenueMarket.market_id` is the `conditionId`, e.g. `"0x064d33..."`.
   The lookup could never hit: measured 0/1918. The
   `or nested_market.get("conditionId")` fallback is dead, because `id`
   is always present, so it never gets the chance to supply the key the
   caller will actually use.

2. NO PAGINATION. `GET /events` was issued bare, and Gamma's default page
   is 20. `_fetch_events` returned 20 events on a listing of hundreds —
   the identical defect already fixed for the sibling `/markets` call,
   left in place on the endpoint next to it.

WHY IT MATTERS ECONOMICALLY. Both venues list ONLY binary markets — 1,918
of 1,918 on Polymarket and 14,028 of 14,028 on Kalshi have exactly two
outcomes — so `multi_outcome_bundle_arbitrage`, which requires
`min_outcomes >= 3` on a single market, cannot fire on live data at all.
The multi-outcome structure that really exists is the EVENT: one Gamma
event holding N binary candidate markets, at most one of which resolves
YES. That is a mutually exclusive set, and buying every NO leg pays at
least `N - 1` no matter what happens, so `sum(no_ask) < N - 1` is an
arbitrage that needs no assumption the listed candidates are exhaustive.
`event_id` is the only thing that recovers those groups, so while it is
`None` the one same-venue trade with NO equivalence risk is unreachable.
"""
import httpx
import pytest

from app.venues.polymarket.adapter import _GAMMA_PAGE_LIMIT, PolymarketAdapter

_CONDITION_A = "0x" + "a" * 64
_CONDITION_B = "0x" + "b" * 64


def _market(condition_id: str, numeric_id: str, question: str) -> dict:
    return {
        "id": numeric_id,
        "conditionId": condition_id,
        "question": question,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.4", "0.6"]',
        "clobTokenIds": f'["{numeric_id}001", "{numeric_id}002"]',
        "endDate": "2027-01-01T00:00:00Z",
        "active": True,
        "closed": False,
    }


def _handler(event_pages: list[list[dict]]):
    """Serve a paginated `/events` and a matching `/markets`."""
    seen: dict[str, int] = {"events": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            offset = int(request.url.params.get("offset", "0"))
            limit = int(request.url.params.get("limit", "20"))
            seen["events"] += 1
            index = offset // limit if limit else 0
            page = event_pages[index] if index < len(event_pages) else []
            return httpx.Response(200, json=page)
        if request.url.path.endswith("/markets"):
            markets = [m for page in event_pages for e in page for m in e["markets"]]
            offset = int(request.url.params.get("offset", "0"))
            limit = int(request.url.params.get("limit", "100"))
            return httpx.Response(200, json=markets[offset : offset + limit])
        return httpx.Response(200, json={})

    return handle, seen


@pytest.mark.asyncio
async def test_event_id_is_keyed_by_the_id_the_caller_looks_up_with() -> None:
    """Defect 1: the mapping must be keyed by `conditionId`."""
    events = [[{
        "id": "2890",
        "markets": [
            _market(_CONDITION_A, "239826", "Will A win?"),
            _market(_CONDITION_B, "239827", "Will B win?"),
        ],
    }]]
    handler, _ = _handler(events)
    adapter = PolymarketAdapter(transport=httpx.MockTransport(handler))

    markets = await adapter.list_markets(status="open")

    assert {m.market_id for m in markets} == {_CONDITION_A, _CONDITION_B}
    # The whole point: every market knows the event it belongs to.
    assert {m.event_id for m in markets} == {"2890"}


@pytest.mark.asyncio
async def test_the_events_listing_is_paginated() -> None:
    """Defect 2: Gamma's default page is 20, and there are far more."""
    # A FULL first page is what makes the fetch ask for a second one, so
    # the pages have to be `_GAMMA_PAGE_LIMIT` long to exercise paging at
    # all — a short page correctly ends the listing.
    def _event(page: int, index: int) -> dict:
        tag = f"{page:02d}{index:03d}"
        return {
            "id": f"e{tag}",
            "markets": [_market("0x" + tag.rjust(64, "0"), tag, f"Q{tag}")],
        }

    pages = [
        [_event(p, i) for i in range(_GAMMA_PAGE_LIMIT)] for p in range(2)
    ] + [[_event(2, 0)]]
    handler, seen = _handler(pages)
    adapter = PolymarketAdapter(transport=httpx.MockTransport(handler))

    markets = await adapter.list_markets(status="open")

    assert len(markets) == 2 * _GAMMA_PAGE_LIMIT + 1
    assert seen["events"] == 3, f"expected 3 /events requests, got {seen['events']}"
    # The whole reason paging matters: without it everything past the
    # first page silently loses its grouping.
    assert all(m.event_id is not None for m in markets)


@pytest.mark.asyncio
async def test_a_standalone_market_keeps_a_null_event_id() -> None:
    """Absence of grouping is not an error — the docstring's contract."""
    events = [[{"id": "2890", "markets": [_market(_CONDITION_A, "239826", "Will A win?")]}]]

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            offset = int(request.url.params.get("offset", "0"))
            return httpx.Response(200, json=events[0] if offset == 0 else [])
        if request.url.path.endswith("/markets"):
            # A market that belongs to no event at all.
            return httpx.Response(
                200,
                json=[_market(_CONDITION_B, "999999", "Standalone?")]
                if request.url.params.get("offset", "0") == "0"
                else [],
            )
        return httpx.Response(200, json={})

    adapter = PolymarketAdapter(transport=httpx.MockTransport(handle))
    markets = await adapter.list_markets(status="open")

    assert [m.event_id for m in markets] == [None]
