"""Kalshi `VenueAdapter` (PLAN.md D3, T12).

`KalshiAdapter` (`adapter.py`) implements the read path — market
metadata, the bids-only order book converted to a normalized two-sided
`OrderBook`, and credentialed balance/positions/orders/fills — over
`httpx.AsyncClient`, with every request signed by `auth.py` (RSA-PSS).
`KalshiLiveAdapter` (`live.py`) is the ONLY place in this package
allowed to place, modify, or cancel a real order (GUARDRAILS.md §1.1).

Units (GUARDRAILS.md §4): Kalshi's integer cents AND fixed-point dollar
strings are both converted to probabilities in `[0.0, 1.0]` inside
`adapter.py` and nowhere else; sizes are contracts.
"""
from app.venues.kalshi.adapter import KalshiAdapter
from app.venues.kalshi.live import KalshiLiveAdapter

__all__ = ["KalshiAdapter", "KalshiLiveAdapter"]
