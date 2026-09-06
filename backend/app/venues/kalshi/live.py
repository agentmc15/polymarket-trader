"""Live Kalshi order placement (GUARDRAILS.md §1.1).

`KalshiLiveAdapter` is the ONLY module (besides `app/venues/polymarket/
live.py`) allowed to place, modify, or cancel a real Kalshi order —
`tests/test_fences.py` (T13) enforces that by AST walk, and T12's
acceptance grep requires the two order-placement paths to appear in this
file and nowhere else under `app/venues/kalshi/`.

Its constructor calls `app.execution.fences.assert_live_allowed()` BEFORE
anything else: constructing this class at all is refused unless
`Settings.trading_mode == "live"` AND `Settings.live_trading_confirmation
== "I_UNDERSTAND_REAL_MONEY"` (PLAN.md D13). GUARDRAILS.md §1.2: never
set either in a test, fixture, or env — this module's tests construct
explicit `Settings` objects and pass them in.

WHICH ENDPOINT EACH DIRECTION USES, AND WHAT IS VERIFIED
--------------------------------------------------------
PLAN.md §3 (docs.kalshi.com, Trade API v2, fetched by the architect
2026-09-04) pins exactly this much of the order request:

    POST /portfolio/events/orders
      ticker, side in {bid, ask}, count (fixed-point string),
      price (dollar string, 2-4 dp),
      time_in_force in {fill_or_kill, good_till_canceled,
                        immediate_or_cancel},
      self_trade_prevention_type,
      optional: client_order_id, post_only, expiration_time

That pin covers a YES-quoted market cleanly — a `bid` buys, an `ask`
sells — but it does NOT say how to express a NO-side order, and
GUARDRAILS.md §1.4 forbids fetching the page that would say
(`kalshi.com`/`kalshi.co` are off limits; the vendor facts were pinned
once and are not re-fetched). So the mapping below is split, and the
UNVERIFIED half is labelled as such rather than guessed at silently:

    outcome  side  endpoint                        body
    -------  ----  ------------------------------  ---------------------
    YES      BUY   POST /portfolio/events/orders   side="bid"
    YES      SELL  POST /portfolio/events/orders   side="ask"
    NO       BUY   POST /portfolio/orders          side="no", action="buy"
    NO       SELL  POST /portfolio/orders          side="no", action="sell"

The NO rows use the LEGACY endpoint deliberately, as the T12 brief
directs when V2 is ambiguous for NO. **The legacy request body's exact
field spelling is UNVERIFIED against live docs** — PLAN.md §3 pins the
V2 body only — so those two rows are marked `verified=False` in the
table below and a wrong field name there will be REJECTED by the venue
(a loud 4xx), not silently mis-executed.

WHY NOT JUST MAP "BUY NO" TO "SELL YES" ON V2? Because they are not the
same order, even though they are the same economic exposure at
complementary prices. Selling YES requires YES contracts you already
hold: neither venue supports naked shorts (PLAN.md §3), so routing a
`BUY NO` as a V2 `ask` on the YES market would be rejected — or, worse,
would liquidate an unrelated YES position the account happened to hold.
The legacy `side: "no"` route buys the NO contract, which is what was
asked for.

`self_trade_prevention_type` is DELIBERATELY OMITTED from the body: it is
named in PLAN.md §3 but its allowed values are not pinned, and inventing
an enum value that silently changes how the venue treats your own
resting orders is exactly the kind of guess this file must not make. The
venue's own default applies until someone can verify the vocabulary.

UNITS (GUARDRAILS.md §4): `OrderRequest.price` is a probability in
`[0,1]` and `OrderRequest.size` is contracts; both are serialized as
fixed-point decimal strings here (`price` at 4 dp, `count` at 2 dp) —
the last place the conversion boundary is crossed on the way out.
"""
from dataclasses import replace
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, NamedTuple

import httpx

from app.config import Settings
from app.execution import fences
from app.venues.base import VenuePayloadError
from app.venues.kalshi.adapter import (
    KalshiAdapter,
    json_object,
    parse_order_ack,
    raise_for_venue_error,
)
from app.venues.types import OrderAck, OrderRequest, TimeInForce

#: Trade API v2 order endpoint (PLAN.md §3). Used for YES-side orders.
V2_ORDERS_PATH = "/portfolio/events/orders"

#: Legacy order endpoint, used for NO-side orders — see the module
#: docstring for why, and for what about it is UNVERIFIED.
LEGACY_ORDERS_PATH = "/portfolio/orders"

#: Decimal places for the serialized `price`. PLAN.md §3 pins "dollar
#: string, 2-4 dp"; 4 is the maximum precision the venue accepts, and it
#: represents every real Kalshi tick (0.01 and the sub-cent ticks in
#: `price_level_structure`) exactly. It also cleans the float noise a
#: derived NO price carries: `1 - 0.58` is `0.42000000000000004` as a
#: double, and quantizing it here yields exactly `"0.4200"`.
_PRICE_DP = 4

#: Decimal places for the serialized `count` (contracts).
_COUNT_DP = 2

#: Normalized `TimeInForce` -> Kalshi's vocabulary (PLAN.md §3).
_TIME_IN_FORCE: dict[TimeInForce, str] = {
    "GTC": "good_till_canceled",
    "IOC": "immediate_or_cancel",
    "FOK": "fill_or_kill",
}


class OrderRoute(NamedTuple):
    """How one `(outcome, side)` direction reaches the venue.

    Attributes:
        path: The endpoint to POST to.
        body_side: The value of the request body's `side` field.
        action: The legacy endpoint's `action` field (`"buy"`/`"sell"`),
            or `None` for the V2 endpoint, which encodes the direction in
            `side` itself.
        verified: Whether this row is backed by the PLAN.md §3 vendor
            pin. `False` means the endpoint and field names are a
            documented, tested ASSUMPTION — see the module docstring.
    """

    path: str
    body_side: str
    action: str | None
    verified: bool


#: `(outcome, side)` -> `OrderRoute`. One row per direction, one unit
#: test per row (`tests/venues/test_kalshi_adapter.py`). Outcome keys are
#: upper-cased before lookup.
ORDER_ROUTES: dict[tuple[str, str], OrderRoute] = {
    ("YES", "BUY"): OrderRoute(V2_ORDERS_PATH, "bid", None, verified=True),
    ("YES", "SELL"): OrderRoute(V2_ORDERS_PATH, "ask", None, verified=True),
    ("NO", "BUY"): OrderRoute(LEGACY_ORDERS_PATH, "no", "buy", verified=False),
    ("NO", "SELL"): OrderRoute(LEGACY_ORDERS_PATH, "no", "sell", verified=False),
}


class KalshiLiveAdapter(KalshiAdapter):
    """Adds real order placement to `KalshiAdapter`.

    The read path (`list_markets`, `get_market`, `get_book`,
    `get_balance`, `get_positions`, `get_open_orders`, `get_fills`,
    `fee_model`) is identical to the parent class — reading market data
    does not change just because this subclass CAN place orders.
    """

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        settings_obj: Settings | None = None,
    ) -> None:
        """Refuse to construct unless live trading is deliberately enabled.

        Args:
            transport: Optional injected `httpx` transport (tests only).
            settings_obj: `Settings` to check the fence against and to
                read base URL/credentials from. Defaults to the
                process-wide `app.config.settings`. GUARDRAILS.md §1.2's
                test pattern is to pass an explicit `Settings(...)` here
                rather than mutate the environment.

        Raises:
            LiveTradingDisabled: Unless both `trading_mode == "live"` and
                `live_trading_confirmation == "I_UNDERSTAND_REAL_MONEY"`
                hold on the checked `Settings`. Checked FIRST, before the
                HTTP client is even built.
        """
        fences.assert_live_allowed(settings_obj)
        super().__init__(transport=transport, settings_obj=settings_obj)

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Issue one signed `POST`. THE ONLY WRITE PATH IN THIS PACKAGE.

        Args:
            path: Endpoint path relative to the configured base URL.
            body: JSON request body.

        Returns:
            dict[str, Any]: The decoded JSON response object.

        Raises:
            VenueAuthError: If credentials are missing, or HTTP 401/403.
            VenueRateLimited: On HTTP 429.
            VenuePayloadError: If the response is not a JSON object.
        """
        request = self._client.build_request("POST", path, json=body)
        self._apply_auth(request, require_auth=True)
        response = await self._client.send(request)
        raise_for_venue_error(response)
        return json_object(response)

    async def place_order(self, order: OrderRequest) -> OrderAck:
        """Place a REAL order on Kalshi. THE ONLY PLACE THIS HAPPENS.

        Routes by `(outcome, side)` through `ORDER_ROUTES` — see the
        module docstring for the full table, which endpoint each
        direction uses, and which half of it is UNVERIFIED against live
        docs.

        Args:
            order: The normalized order. `price` is a probability in
                `[0,1]` FOR THE OUTCOME BEING TRADED (a `BUY NO` at 0.58
                means 58c for a NO contract, not 58c for YES), and
                `size` is contracts.

        Returns:
            OrderAck: Parsed from the venue's response. When the venue
                echoes no `client_order_id`, the request's is carried
                through, so the idempotency key never goes missing
                between request and ack (PLAN.md D4).

        Raises:
            VenuePayloadError: If `order.outcome` is not YES/NO or
                `order.side` is not BUY/SELL — an unroutable order is
                refused here rather than sent somewhere plausible.
            VenueAuthError: If Kalshi credentials are not configured.
        """
        key = (order.outcome.strip().upper(), order.side.strip().upper())
        route = ORDER_ROUTES.get(key)
        if route is None:
            raise VenuePayloadError(
                f"no Kalshi order route for outcome={order.outcome!r} "
                f"side={order.side!r}",
                raw={"outcome": order.outcome, "side": order.side},
            )
        body = build_order_body(order, route)
        payload = await self._post(route.path, body)
        ack = parse_order_ack(payload)
        echoed = payload.get("order") if isinstance(payload.get("order"), dict) else payload
        if not (isinstance(echoed, dict) and echoed.get("client_order_id")):
            ack = replace(ack, client_order_id=order.client_order_id)
        return ack

    async def cancel_order(self, order_id: str) -> None:
        """Cancel a REAL resting order. THE ONLY PLACE THIS HAPPENS.

        Args:
            order_id: Venue-native order identifier to cancel.

        Raises:
            VenueAuthError: If Kalshi credentials are not configured, or
                the venue rejects the signed request.
            VenueRateLimited: On HTTP 429.
        """
        request = self._client.build_request(
            "DELETE", f"{LEGACY_ORDERS_PATH}/{order_id}"
        )
        self._apply_auth(request, require_auth=True)
        response = await self._client.send(request)
        raise_for_venue_error(response)


def build_order_body(order: OrderRequest, route: OrderRoute) -> dict[str, Any]:
    """Serialize one `OrderRequest` into a Kalshi order body.

    Split out of `place_order` so the exact wire body for all four
    routes can be unit-tested without an adapter, a transport, or the
    live fence — the tests assert the body, not a mock's call args.

    `price` and `count` are FIXED-POINT DECIMAL STRINGS (PLAN.md §3), not
    floats: `price` at `_PRICE_DP` (4) decimal places, `count` at
    `_COUNT_DP` (2). `post_only` is emitted only when actually requested,
    so a `False` never has to be understood by the legacy endpoint, whose
    field set is unverified.

    Args:
        order: The normalized order.
        route: The `ORDER_ROUTES` row for this direction.

    Returns:
        dict[str, Any]: The JSON body to POST.
    """
    body: dict[str, Any] = {
        "ticker": order.market_id,
        "side": route.body_side,
        "count": _fixed_point(order.size, _COUNT_DP),
        "price": _fixed_point(order.price, _PRICE_DP),
        "time_in_force": _TIME_IN_FORCE[order.tif],
        "client_order_id": order.client_order_id,
    }
    if route.action is not None:
        body["action"] = route.action
    if order.post_only:
        body["post_only"] = True
    return body


def _fixed_point(value: float, places: int) -> str:
    """Render `value` as a fixed-point decimal string with `places` digits.

    The value is first rounded to `places + 6` digits to discard IEEE-754
    representation noise (the same problem, and the same remedy, as
    `app/venues/fees.py::_ceil_decimal` documents at length: a derived
    price like `1 - 0.58` is `0.42000000000000004` as a double, and
    `str()` on it faithfully reports every one of those digits). Only
    then is it quantized to `places`, so a value that was conceptually
    already exact does not drift by a tick on the way to the venue.

    ROUND_HALF_EVEN is used for the final quantize. It is only ever
    reachable for a price that is genuinely off-tick at `places` digits —
    every real Kalshi tick (0.01, and the sub-cent ticks
    `price_level_structure` can specify) is exactly representable at 4
    decimal places, so no rounding occurs for a well-formed order.

    Args:
        value: The number to render, finite (`OrderRequest` validates).
        places: Decimal places in the output.

    Returns:
        str: e.g. `"0.4200"` for `(0.42, 4)`, `"100.00"` for `(100, 2)`.
    """
    quantum = Decimal(1).scaleb(-places)
    denoised = Decimal(str(round(value, places + 6)))
    return str(denoised.quantize(quantum, rounding=ROUND_HALF_EVEN))
