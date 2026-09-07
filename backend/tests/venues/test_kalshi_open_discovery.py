"""`list_markets(status="open")` discovers via `/events`, not `/markets`.

`GET /markets` returns an unordered global listing dominated by
synthetic `KXMVECROSSCATEGORY` multi-leg markets. Measured against the
live API with credentials: of the 10,000 markets our pagination cap
allows, **2 carried a live bid**. Real series were quoting the whole
time — `KXFEDDECISION` 58 of 65, `KXNFLGAME` 32 of 100 — but sit below
the cap, so the scanner never reached one tradeable Kalshi market and
cross-venue arbitrage could not fire at all.

`GET /events?status=open&with_nested_markets=true` is the same data
selected usefully. Live, after this change: 14,028 markets of which
12,571 carry a live bid (89%, against 0.02%), in 1.8s.

`status=None` still uses the flat listing, deliberately:
`near_resolution_pass` passes `None` precisely because it must read
markets whose `close_time` has already passed, and those are not open
events.
"""
from typing import Any

import httpx
import pytest

from app.venues.kalshi.adapter import KalshiAdapter
from tests.venues.test_kalshi_adapter import MARKETS, make_settings


def _transport(seen: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/events"):
            assert request.url.params.get("status") == "open"
            assert request.url.params.get("with_nested_markets") == "true"
            return httpx.Response(
                200,
                json={"events": [{"event_ticker": "EV1", "markets": list(MARKETS)}],
                      "cursor": ""},
            )
        if request.url.path.endswith("/markets"):
            return httpx.Response(200, json={"markets": [], "cursor": ""})
        return httpx.Response(404, json={"error": "unrouted"})

    return httpx.MockTransport(handler)


def _adapter(seen: list[str]) -> KalshiAdapter:
    return KalshiAdapter(transport=_transport(seen), settings_obj=make_settings())


@pytest.mark.asyncio
async def test_open_discovery_reads_events_and_not_the_flat_listing() -> None:
    seen: list[str] = []

    markets = await _adapter(seen).list_markets(status="open")

    assert markets, "nested event markets must be returned"
    assert any(p.endswith("/events") for p in seen)
    assert not any(p.endswith("/markets") for p in seen), (
        "the flat listing is 99.98% untradeable and truncates before the liquid "
        "series; open discovery must not use it"
    )


@pytest.mark.asyncio
async def test_status_none_still_uses_the_flat_listing() -> None:
    """near_resolution_pass depends on it: past-close markets are not open events."""
    seen: list[str] = []

    await _adapter(seen).list_markets(status=None)

    assert any(p.endswith("/markets") for p in seen)
    assert not any(p.endswith("/events") for p in seen)


@pytest.mark.asyncio
async def test_an_event_carrying_no_markets_is_not_fatal() -> None:
    """Events legitimately arrive with `markets` absent or null."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                json={
                    "events": [
                        {"event_ticker": "EMPTY"},
                        {"event_ticker": "NULL", "markets": None},
                        {"event_ticker": "OK", "markets": list(MARKETS)},
                    ],
                    "cursor": "",
                },
            )
        return httpx.Response(404, json={"error": "unrouted"})

    adapter = KalshiAdapter(
        transport=httpx.MockTransport(handler), settings_obj=make_settings()
    )

    markets: list[Any] = await adapter.list_markets(status="open")

    # The two malformed events must not stop the good one's markets coming
    # through. The count is not len(MARKETS): one fixture market is
    # legitimately skipped by the skip-and-count contract, which is the
    # behaviour under test elsewhere.
    assert markets, "an event with no markets must not blank the listing"
    tickers = {m["ticker"] for m in MARKETS}
    assert {m.market_id for m in markets} <= tickers
