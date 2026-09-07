"""Resolved markets must come from the event listing too, not the flat one.

T-era work moved OPEN discovery from `GET /markets` to
`GET /events?with_nested_markets=true` for a measured reason: the flat
listing is dominated by synthetic `KXMVECROSSCATEGORY` multi-leg shards,
so of 10,000 markets fetched (the pagination cap) 2 carried a live bid.
Only the `status == "open"` branch was moved. Every other status kept
going to the flat listing, and it is exactly as bad there:

    list_markets(status="resolved")
      -> 10,000 markets, 10,000 of them MVE shards (100%)
      -> 0 with any volume, 0 usable for anything

Those shards are created and settled seconds apart with zero volume,
zero open interest and zero liquidity, and there are enough of them to
fill the cap on their own. The real settled markets sit below it.

The same `/events` walk with the venue's own status word returns 139,220
settled markets with ZERO shards, 64,879 of them carrying real volume and
a price strictly inside (0, 1), in 12.8 seconds.

This matters beyond tidiness: settled markets with prices are the only
historical dataset either venue exposes, and therefore the only way to
test whether prices are calibrated — the question behind every
non-arbitrage thesis in this kit. While this returned 100% shards, that
entire line of work was blocked by what looked like a venue limitation.
"""
import pytest

from app.venues.kalshi.adapter import KalshiAdapter


class _Recorder:
    """Records which endpoint was asked, and serves both shapes."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.statuses: list[str | None] = []

    async def get(self, path: str, params: dict | None = None) -> dict:
        self.paths.append(path)
        self.statuses.append((params or {}).get("status"))
        market = {
            "ticker": "KXREAL-1",
            "title": "Did it happen?",
            "close_time": "2026-01-01T00:00:00Z",
            "status": "finalized",
            "result": "yes",
            "volume_fp": "5000.00",
            "last_price_dollars": "0.6000",
        }
        if path == "/events":
            return {"events": [{"event_ticker": "KXREAL", "markets": [market]}],
                    "cursor": ""}
        # The flat listing, standing in for the shard-dominated reality.
        return {"markets": [{
            "ticker": "KXMVECROSSCATEGORY-SHARD1-S1-A1",
            "title": "synthetic shard",
            "close_time": "2026-01-01T00:00:00Z",
            "status": "finalized",
            "result": "no",
            "volume_fp": "0.00",
        }], "cursor": ""}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "venue_word"),
    [("resolved", "settled"), ("closed", "closed"), ("open", "open")],
)
async def test_every_status_is_discovered_through_the_event_listing(
    status: str, venue_word: str
) -> None:
    adapter = KalshiAdapter()
    rec = _Recorder()
    adapter._get = rec.get  # type: ignore[method-assign]

    markets = await adapter.list_markets(status=status)  # type: ignore[arg-type]

    assert rec.paths and set(rec.paths) == {"/events"}, (
        f"status={status!r} still went to {set(rec.paths) - {'/events'}}"
    )
    # The venue's own vocabulary is what goes on the wire.
    assert rec.statuses[0] == venue_word
    assert all("MVECROSS" not in m.market_id for m in markets)


@pytest.mark.asyncio
async def test_the_resolved_walk_returns_the_real_market_not_a_shard() -> None:
    adapter = KalshiAdapter()
    rec = _Recorder()
    adapter._get = rec.get  # type: ignore[method-assign]

    markets = await adapter.list_markets(status="resolved")

    assert [m.market_id for m in markets] == ["KXREAL-1"]
    assert markets[0].result == "yes"
