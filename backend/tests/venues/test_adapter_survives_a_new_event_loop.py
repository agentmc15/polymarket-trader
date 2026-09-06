"""An adapter must survive a SECOND `asyncio.run` in the same process.

This is the shape of a Celery worker. Every beat entry point is
`asyncio.run(<coro>)`, and a worker process handles many ticks, so a new
event loop is created and closed for each one. In paper mode
`make_paper_adapter` deliberately returns a PROCESS-WIDE singleton --
its in-memory orders are the only record of a paper fill there is -- so
one adapter instance spans all of those loops.

An `httpx.AsyncClient` holds connections bound to the loop that opened
them. Before the fix, tick 1 of a beat succeeded and every tick
afterwards died inside the transport with `RuntimeError: Event loop is
closed`, permanently: the dead pool was reused forever and retrying
could not heal it. Observed directly against the live APIs.

These tests assert the REBINDING, not a socket failure, and that
distinction is the point. An `httpx.MockTransport` has no sockets and
no SSL, so it does not care which loop it runs on: a test that merely
calls `asyncio.run` twice and expects an error PASSES against the
broken code. That exact test was written first here and it was
vacuous. Only a real network transport reproduces the crash, and
GUARDRAILS.md §1.4 forbids one.

So what is pinned instead is the mechanism that fixes it -- that a new
running loop causes a new client object, and that the injected
transport survives the swap. That is checkable without a network and it
fails when the guard is removed.
"""
import asyncio

import httpx
import pytest

from app.venues.kalshi.adapter import KalshiAdapter
from app.venues.polymarket.adapter import PolymarketAdapter
from tests.venues.test_polymarket_adapter import _make_transport as _poly_transport


def _kalshi_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/markets"):
            return httpx.Response(200, json={"markets": [], "cursor": ""})
        return httpx.Response(404, json={"error": "unrouted"})

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    ("label", "build"),
    [
        ("polymarket", lambda: PolymarketAdapter(transport=_poly_transport())),
        ("kalshi", lambda: KalshiAdapter(transport=_kalshi_transport())),
    ],
)
def test_a_new_event_loop_gets_a_new_client(label, build) -> None:
    """One adapter, three `asyncio.run` calls: three distinct clients.

    Reusing the client across loops is the defect. In production its
    pooled connections belong to a loop `asyncio.run` has already
    closed, so every beat tick after the first dies in the transport.
    """
    adapter = build()
    attr = "_gamma" if label == "polymarket" else "_http"
    seen = []

    for _ in range(3):
        asyncio.run(adapter.list_markets())
        seen.append(id(getattr(adapter, attr)))

    assert len(set(seen)) == 3, (
        f"{label} reused one httpx client across three event loops "
        f"(ids={seen}); in a Celery worker every tick after the first "
        "would raise 'Event loop is closed', permanently."
    )


def test_the_injected_transport_survives_a_rebuild() -> None:
    """A rebuilt client must NOT fall back to the real network.

    The rebuild path constructs a fresh `httpx.AsyncClient`. If it
    dropped the injected transport, a test would start reaching
    Polymarket for real -- GUARDRAILS.md §1.4 -- and would look like it
    was passing while doing so.
    """
    transport = _poly_transport()
    adapter = PolymarketAdapter(transport=transport)

    asyncio.run(adapter.list_markets())
    asyncio.run(adapter.list_markets())

    assert adapter._gamma._transport is transport
    assert adapter._clob._transport is transport
