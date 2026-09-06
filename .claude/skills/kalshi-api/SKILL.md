---
name: kalshi-api
description: Kalshi Trade API v2 integration facts, endpoints, and fee model. Reference for venue adapter implementation (app/venues/kalshi/).
---

# Kalshi Trade API v2 — Vendor Facts

Pinned from docs.kalshi.com on 2026-09-04. Do NOT re-fetch from the network (GUARDRAILS.md §1.4).

## Base URLs

- **Production**: `https://external-api.kalshi.com/trade-api/v2` (alt: `https://api.elections.kalshi.com/trade-api/v2`)
- **Demo**: `https://external-api.demo.kalshi.co/trade-api/v2`
- **WebSocket** (NOT used in this kit): `wss://external-api-ws.kalshi.com/trade-api/ws/v2`

## Authentication

Signature-based with RSA private key:

- Headers: `KALSHI-ACCESS-KEY` (public key ID), `KALSHI-ACCESS-TIMESTAMP` (ms), `KALSHI-ACCESS-SIGNATURE`
- Signature algorithm: RSA-PSS(SHA256, MGF1-SHA256, salt length = digest length)
- Signed string: `f"{timestamp_ms}{METHOD}{path}"` where `path` is URL path WITHOUT query string
- Example: sign `/trade-api/v2/portfolio/orders`, not `…?status=open`
- Private key format: PEM-encoded

## Market Payload

Fixed-point dollar strings for prices and sizes (some older payloads use integer cents; parse both):

- `yes_bid_dollars`, `yes_ask_dollars`, `no_bid_dollars`, `no_ask_dollars` — top-of-book bids/asks
- `last_price_dollars` — most recent trade price
- `*_fp` fields — sizes (fixed-point, contract count)
- `rules_primary`, `rules_secondary` — market rules text
- `close_time`, `expected_expiration_time`, `latest_expiration_time` — timing fields
- `settlement_timer_seconds` — dispute-window countdown
- `settlement_ts` — settlement timestamp
- `result` — one of `{yes, no, scalar, ""}` (empty = unresolved)
- `fee_waiver_expiration_time` — optional fee waiver deadline
- `price_level_structure` — tick sizes by price range

## Order Book

`GET /markets/{ticker}/orderbook` returns:

```json
{
  "orderbook_fp": {
    "yes_dollars": [[price, qty], ...],
    "no_dollars": [[price, qty], ...]
  }
}
```

- **Bids only**: Both fields are bid levels
- **Derive asks**: A NO bid at price q is a YES ask at 1−q
- **Derivation**: `yes_asks = 1 - no_dollars`, `no_asks = 1 - yes_dollars`

## Orders (V2)

Used for YES-side orders only (`side="bid"` to buy, `side="ask"` to sell —
see the Legacy section below for why NO-side orders and all cancels use a
different endpoint).

`POST /portfolio/events/orders` with:

- `ticker` — market ticker
- `side` — one of `{bid, ask}` (not BUY/SELL)
- `count` — size, fixed-point string (contract count)
- `price` — limit price, dollar string (2–4 decimal places)
- `time_in_force` — one of `{fill_or_kill, good_till_canceled, immediate_or_cancel}`
- `self_trade_prevention_type` — optional
- `client_order_id` — optional, for idempotency
- `post_only` — optional, post-only limit flag
- `expiration_time` — optional

Response includes:

- `order_id`, `client_order_id`, `fill_count`, `remaining_count`
- `average_fill_price`, `average_fee_paid`
- `ts_ms` — server timestamp (milliseconds)

## Orders (Legacy — NO side and all cancels)

`app/venues/kalshi/live.py` also calls two legacy endpoints, both marked
`verified=False` in that file's `ORDER_ROUTES` table because GUARDRAILS.md
§1.4 forbids fetching kalshi.com/kalshi.co to confirm their exact
body/response shape — the field spellings below are a documented,
tested ASSUMPTION, not a vendor-pinned fact:

- `POST /portfolio/orders` — used for NO-side orders instead of V2:
  `side="no"` plus `action` (`"buy"` or `"sell"`). This is deliberate, not
  a shortcut — buying/selling NO is a different contract from YES, not a
  mirrored `ask`, and neither venue supports naked shorts, so routing
  "buy NO" as a V2 YES `ask` would either be rejected or (worse) liquidate
  an unrelated YES position the account happened to hold.
- `DELETE /portfolio/orders/{order_id}` — used for ALL cancels, YES and
  NO alike; there is no V2 cancel path in this adapter.

A wrong field name on either legacy call is expected to surface as a loud
4xx from the venue, not a silent misexecution.

## Fees

**Formula**: `fee = rate × C × P × (1 − P)` where C = contracts, P = probability in [0, 1]

- **Standard rates**: Taker 0.07, Maker 0.0 (some series carry maker fees)
- **Rounding**: `trade_fee = ceil_6dp(model_fee)` per fill, then `ceil_2dp(net)` for non-direct members
- **Per-fill ceiling**: Applies to each partial fill independently; multi-fill orders sum multiple ceilings
- **Fee waiver**: Some markets carry `fee_waiver_expiration_time` (zero fee until deadline)
- **Rates are NOT confirmed at implementation time** (PLAN.md D11; the 0.07 default is historical)

## Capital

- **Account type**: USD in CFTC-regulated FCM account
- **Deposit/withdrawal**: ACH/wire transfers (days, not instant)
- **Fungibility**: NOT fungible with Polymarket USDC in real time
- **Transfer latency**: Default `transfer_latency_hours=72`
- **Per-venue ledgers**: Capital does not auto-move between venues; each venue has its own balance and position tracking

## Key Implementation Notes

1. The field name decides the encoding, never the magnitude: bare
   `yes`/`no` is the LEGACY integer-cents format (divide by 100 to get a
   probability); `yes_dollars`/`no_dollars` is the current fixed-point
   dollar-string format (already a probability in `[0,1]`, no
   conversion). Both can appear regardless of whether the payload's top
   level key was `orderbook_fp` or `orderbook` — check the field suffix
   inside the container, not the container's own key name.
2. Derive YES asks from NO bids and vice versa (orderbook is bids-only)
3. Prices are probabilities in [0, 1]; no conversion to fixed-point in the adapter
4. Sizes are contracts (1 = $1 at resolution); stored as float
5. Fee model must round per-fill to 6 decimal places, then optionally to cents
6. Signature must be computed at request time with fresh timestamp_ms
7. REST polling only; WebSocket not implemented in this kit (raises `NotImplementedError`)
