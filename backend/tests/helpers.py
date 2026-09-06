"""Shared test-construction helpers.

Kept separate from `conftest.py` so strategy/engine tests can import the
builders directly without pulling in the FastAPI `client`/DB fixtures.
"""
from datetime import datetime
from typing import Any

from app.strategies.base import MarketSnapshot
from app.utils.time import utcnow
from app.venues.types import BookLevel, OrderBook, VenueId


def make_snapshot(
    market_id: str = "m1",
    ts: datetime | None = None,
    yes: float = 0.5,
    no: float | None = None,
    spread: float = 0.02,
    volume_24h: float = 50_000.0,
    end_date: datetime | None = None,
    **kw: Any,
) -> MarketSnapshot:
    """Build a `MarketSnapshot` with sane, aware-UTC-by-default test values.

    Args:
        market_id: Market condition ID.
        ts: Snapshot timestamp. Defaults to `utcnow()` (aware UTC) — never
            a naive `datetime`, per GUARDRAILS.md's datetime convention.
            This helper does NOT itself call `ensure_aware()` on a
            caller-supplied `ts`: `MarketSnapshot.__post_init__` (T06,
            PLAN.md R9) is the single source of truth for that validation,
            so a naive `ts` raises `ValueError` from the `MarketSnapshot`
            constructor this function calls, not from a duplicated check
            here. A test that deliberately wants a `MarketSnapshot`
            carrying a naive timestamp (e.g. to prove the domain type
            itself rejects one) must construct `MarketSnapshot(...)`
            directly instead of going through this helper.
        yes: YES token price.
        no: NO token price. Defaults to `1.0 - yes` (arbitrage-neutral: a
            default `make_snapshot(yes=0.6)` yields `yes + no == 1.0`, not
            a fabricated complement edge) — pass an explicit `no=` for
            tests that deliberately want a complement violation.
        spread: Bid/ask spread used to derive `yes_bid`/`yes_ask`/`no_bid`/
            `no_ask` symmetrically around `yes`/`no`, clamped to
            `[0.0, 1.0]` so an extreme `yes`/`spread` combination (e.g.
            `yes=0.99, spread=0.05`) never produces an out-of-range price.
        volume_24h: 24-hour trading volume.
        end_date: Optional market end date (aware UTC if provided).
        **kw: Any other `MarketSnapshot` field, overriding the computed
            defaults above (including `token_id`, `yes_bid`, etc.).

    Returns:
        MarketSnapshot: A fully populated snapshot instance.
    """
    timestamp = ts if ts is not None else utcnow()
    no_price = no if no is not None else 1.0 - yes

    def _clamp(value: float) -> float:
        return max(0.0, min(1.0, value))

    fields: dict[str, Any] = {
        "market_id": market_id,
        "token_id": f"{market_id}_yes",
        "timestamp": timestamp,
        "yes_price": yes,
        "no_price": no_price,
        "yes_bid": _clamp(yes - spread / 2),
        "yes_ask": _clamp(yes + spread / 2),
        "no_bid": _clamp(no_price - spread / 2),
        "no_ask": _clamp(no_price + spread / 2),
        "spread": spread,
        "volume_24h": volume_24h,
        "question": f"Sample market {market_id}?",
        "category": "test",
        "end_date": end_date,
    }
    fields.update(kw)
    return MarketSnapshot(**fields)


def make_book(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    venue: VenueId = "polymarket",
    market_id: str = "m1",
    outcome: str = "YES",
    ts: datetime | None = None,
) -> OrderBook:
    """Build an `OrderBook` fixture.

    Args:
        bids: List of `(price, size)` bid levels, best price first.
        asks: List of `(price, size)` ask levels, best price first.
        venue: Venue name (e.g. `"polymarket"`, `"kalshi"`).
        market_id: Market identifier.
        outcome: Outcome side, `"YES"` or `"NO"`.
        ts: Book timestamp. Defaults to `utcnow()` (aware UTC).

    Returns:
        OrderBook: A normalized order book with `bids`/`asks` as
            `BookLevel` instances.
    """
    return OrderBook(
        venue=venue,
        market_id=market_id,
        outcome=outcome,
        bids=[BookLevel(price=price, size=size) for price, size in bids],
        asks=[BookLevel(price=price, size=size) for price, size in asks],
        ts=ts if ts is not None else utcnow(),
    )
