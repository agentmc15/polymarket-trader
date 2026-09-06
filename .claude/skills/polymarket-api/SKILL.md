---
name: polymarket-api
description: Deep integration guide for Polymarket's CLOB API, Gamma API, and on-chain data. Use when building trading functionality, fetching market data, or implementing order execution.
---

# Polymarket API Integration Skill

## Overview

This skill provides comprehensive guidance for integrating with Polymarket's APIs and smart contracts.

## API Endpoints

### CLOB API (Central Limit Order Book)
Base URL: `https://clob.polymarket.com`

#### Authentication Levels
- **Level 0 (Public)**: Market data, orderbooks, prices
- **Level 1 (Signer)**: Create/derive API keys
- **Level 2 (Authenticated)**: Trading, orders, positions

#### Key Endpoints
```
GET  /markets              # List all markets
GET  /markets/{condition_id}  # Get specific market (condition_id is the
                               # MARKET key; token_id is the per-outcome
                               # BOOK key used by /price, /midpoint, /book
                               # below — the two are not interchangeable)
GET  /price?token_id=X     # Get current price
GET  /midpoint?token_id=X  # Get midpoint price
GET  /book?token_id=X      # Get orderbook
GET  /trades               # Get user trades
POST /order                # Place order
DELETE /order/{id}         # Cancel order
GET  /positions            # Get positions
```

### Gamma API (Market Metadata)
Base URL: `https://gamma-api.polymarket.com`

```
GET /events              # List events
GET /events/{slug}       # Get event details
GET /markets             # List markets
GET /markets/{id}        # Get market details
```

## Python Implementation Patterns

### Initialize Client
```python
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
import os

class PolymarketService:
    def __init__(self):
        self.client = ClobClient(
            host="https://clob.polymarket.com",
            key=os.getenv("POLYMARKET_PRIVATE_KEY"),
            chain_id=137,
            signature_type=1,
            funder=os.getenv("POLYMARKET_FUNDER_ADDRESS")
        )
        self.client.set_api_creds(
            self.client.create_or_derive_api_creds()
        )
    
    async def get_market_data(self, token_id: str) -> dict:
        """Fetch comprehensive market data."""
        return {
            "price": self.client.get_price(token_id, "BUY"),
            "midpoint": self.client.get_midpoint(token_id),
            "book": self.client.get_order_book(token_id),
            "spread": self.client.get_spread(token_id),
        }
    
    async def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str = "GTC"
    ) -> dict:
        """Place a limit order."""
        order = self.client.create_order(
            OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side,
            )
        )
        return self.client.post_order(order, order_type)
```

### WebSocket Subscription
```python
import asyncio
import websockets
import json

async def subscribe_market_updates(token_ids: list[str]):
    """Subscribe to real-time market updates."""
    uri = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({
            "type": "subscribe",
            "markets": token_ids
        }))
        
        async for message in ws:
            data = json.loads(message)
            yield data
```

### Gamma API Client
```python
import httpx

class GammaClient:
    BASE_URL = "https://gamma-api.polymarket.com"
    
    def __init__(self):
        self.client = httpx.AsyncClient(base_url=self.BASE_URL)
    
    async def get_active_markets(self) -> list[dict]:
        """Fetch all active markets."""
        response = await self.client.get("/markets", params={"active": True})
        return response.json()
    
    async def get_event(self, slug: str) -> dict:
        """Fetch event with all markets."""
        response = await self.client.get(f"/events/{slug}")
        return response.json()
```

## Order Types

- **GTC** (Good Till Cancelled): Stays until filled or cancelled
- **GTD** (Good Till Date): Expires at specified time
- **FOK** (Fill or Kill): Must fill entirely or cancel
- **IOC** (Immediate or Cancel): Fill what's available, cancel rest

## Fee Model

**Polymarket's taker-only formula** (docs.polymarket.com/trading/fees, fetched 2026-09-04):

```
fee = C × rate × p × (1 − p)
```

Where:
- `C` = contract count ($1 per contract at resolution)
- `rate` = taker fee rate (category-dependent, makers never pay)
- `p` = fill price, a probability in [0, 1]
- `(1 − p)` = the fee term; maximized at p=0.5 (coin-flip), vanishes at tails (p→0 or p→1)

**Category taker rates** (charged in USDC):

| Category | Rate |
|----------|------|
| Crypto | 0.07 |
| Sports / Economics / Culture / Weather / Other | 0.05 |
| Finance / Politics / Mentions / Tech | 0.04 |
| Geopolitics | 0.00 |

**Per-market override**: The CLOB market payload may carry `maker_base_fee`/`taker_base_fee` (historical field names), which is authoritative over the category table when present. These fields are in BASIS POINTS, not a dimensionless rate — `taker_base_fee=700` means 0.07, not 700. Divide by `10_000` to get the rate `FeeSchedule` expects (`app/venues/polymarket/adapter.py` does this at the two places it reads these fields) before building a `FeeSchedule` from the payload (`source="clob_market"`) rather than from `category_rate()`. Skipping the division is a 10,000× fee error.

**Maker fees**: Always 0 on Polymarket CLOB; taker-only venue.

## Price Calculations

```python
def calculate_implied_probability(price: float) -> float:
    """Convert price to implied probability."""
    return price  # Prices ARE probabilities (0-1)

def calculate_cost(price: float, shares: float) -> float:
    """Calculate cost to buy shares."""
    return price * shares

def calculate_pnl(
    entry_price: float,
    current_price: float,
    shares: float,
    side: str
) -> float:
    """Calculate unrealized P&L."""
    if side == "BUY":
        return (current_price - entry_price) * shares
    return (entry_price - current_price) * shares
```

## Error Handling

```python
from py_clob_client.exceptions import PolymarketException

try:
    result = client.post_order(order)
except PolymarketException as e:
    if "INSUFFICIENT_BALANCE" in str(e):
        # Handle insufficient funds
        pass
    elif "INVALID_PRICE" in str(e):
        # Handle price out of range
        pass
    raise
```

## Rate Limits

- Public endpoints: ~100 requests/minute
- Authenticated endpoints: ~1000 requests/minute
- WebSocket: Varies by subscription type

Always implement exponential backoff and request queuing.

## Key Contract Addresses (Polygon)

```python
CONTRACTS = {
    "CTF_EXCHANGE": "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
    "NEG_RISK_CTF_EXCHANGE": "0xC5d563A36AE78145C45a50134d48A1215220f80a",
    "CONDITIONAL_TOKENS": "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045",
    "USDC": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
}
```
