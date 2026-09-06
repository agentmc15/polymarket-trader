"""Live Polymarket order placement (GUARDRAILS.md §1.1).

`PolymarketLiveAdapter` is the ONLY module (besides `app/venues/kalshi/
live.py`, T12) allowed to call `ClobClientWrapper.create_order`/
`post_order`/`cancel_order` — `tests/test_fences.py` (T13) enforces this
by AST walk. Its constructor calls `app.execution.fences.
assert_live_allowed()` BEFORE doing anything else: constructing this
class at all is refused unless `Settings.trading_mode == "live"` AND
`Settings.live_trading_confirmation == "I_UNDERSTAND_REAL_MONEY"`
(PLAN.md D13). GUARDRAILS.md §1.2: never set those in a test, fixture, or
env — this module's own tests construct explicit `Settings` objects and
pass them to the constructor instead.

KNOWN, PRE-EXISTING LIMITATION (out of T11's scope, flagged for T13/T16):
`ClobClientWrapper.create_order` (`app/services/polymarket/client.py`)
calls `self.client.create_order(token_id=..., price=..., size=...,
side=...)` — but the installed `py_clob_client`'s `ClobClient.create_order`
signature is `create_order(order_args: OrderArgs, options=None)`, not
those four keywords. Calling `place_order` today would raise `TypeError`
from that mismatch before any network request is made — a safety-positive
failure mode (it fails loud, not silently wrong), but a genuine
pre-existing defect that predates this file and is not fixed here: T11's
brief scopes the executor fix to `app/bots/executor.py`'s `signal.side`
bug only. Similarly, `ClobClientWrapper.post_order` takes no `order_type`
argument, so a live order always posts using the underlying client's own
default (`OrderType.GTC`) regardless of the normalized `OrderRequest.tif`
requested — FOK/IOC are accepted on `OrderRequest` but not honored
end-to-end until `ClobClientWrapper` itself is corrected.
"""
import asyncio
from typing import Any, Literal

import httpx

from app.config import Settings
from app.execution import fences
from app.utils.time import utcnow
from app.venues.base import VenuePayloadError
from app.venues.polymarket.adapter import PolymarketAdapter
from app.venues.types import OrderAck, OrderRequest


class PolymarketLiveAdapter(PolymarketAdapter):
    """Adds real order placement to `PolymarketAdapter`.

    The read path (`list_markets`, `get_market`, `get_book`, `get_balance`,
    `get_positions`, `get_open_orders`, `get_fills`, `fee_model`) is
    identical to the parent class — nothing about reading market data
    changes just because this subclass CAN place orders. Only this
    subclass may place, modify, or cancel a real Polymarket order.
    """

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        settings_obj: Settings | None = None,
    ) -> None:
        """Refuse to construct unless live trading is deliberately enabled.

        Args:
            transport: Optional injected `httpx` transport (tests only).
            settings_obj: `Settings` instance to check the fence against.
                Defaults to the process-wide `app.config.settings`
                singleton. GUARDRAILS.md §1.2's test pattern is to pass an
                explicit `Settings(...)` here rather than mutate the
                environment.

        Raises:
            LiveTradingDisabled: Unless both `trading_mode == "live"` and
                `live_trading_confirmation == "I_UNDERSTAND_REAL_MONEY"`
                hold on the checked `Settings`.
        """
        fences.assert_live_allowed(settings_obj)
        super().__init__(transport=transport)

    async def place_order(self, order: OrderRequest) -> OrderAck:
        """Place a REAL order on Polymarket. THE ONLY PLACE THIS HAPPENS.

        Resolves `order.outcome` to a CLOB `token_id` via `get_market`
        (same resolution `get_book` uses), then calls
        `ClobClientWrapper.create_order` and `.post_order` — both
        declared `async def` but internally synchronous (PLAN.md §3), so
        each is driven via `self._run_coroutine` inside
        `asyncio.to_thread`, exactly like every other credentialed call
        in the parent class.

        Args:
            order: The normalized order to place.

        Returns:
            OrderAck: Parsed from the CLOB `post_order` response.

        Raises:
            VenueAuthError: If `settings.polymarket_private_key` is empty.
            VenuePayloadError: If `order.outcome` is not a known outcome
                of `order.market_id`, or the post-order response is
                malformed.
        """
        wrapper = await self._ensure_clob_wrapper()
        market = await self.get_market(order.market_id)
        try:
            token_id = market.outcome_ids[order.outcome]
        except KeyError:
            raise VenuePayloadError(
                f"unknown outcome {order.outcome!r} for market {order.market_id!r}",
                raw=dict(market.outcome_ids),
            ) from None
        signed: Any = await asyncio.to_thread(
            self._run_coroutine,
            lambda: wrapper.create_order(
                token_id=token_id,
                price=order.price,
                size=order.size,
                side=order.side,
            ),
        )
        response: Any = await asyncio.to_thread(
            self._run_coroutine, lambda: wrapper.post_order(signed)
        )
        return _parse_place_order_response(response, order)

    async def cancel_order(self, order_id: str) -> None:
        """Cancel a REAL resting order. THE ONLY PLACE THIS HAPPENS.

        Args:
            order_id: Venue-native order identifier to cancel.

        Raises:
            VenueAuthError: If `settings.polymarket_private_key` is empty.
        """
        wrapper = await self._ensure_clob_wrapper()
        await asyncio.to_thread(self._run_coroutine, lambda: wrapper.cancel_order(order_id))


def _parse_place_order_response(response: object, order: OrderRequest) -> OrderAck:
    """Best-effort parse of a CLOB `post_order` response into an `OrderAck`.

    Not pinned by PLAN.md §3 to an exact schema; treats a truthy
    `success` (default `True`, matching a normal placement) as `"open"`
    and a falsy one as `"rejected"`. `filled_size` is conservatively `0.0`
    — a real fill confirmation should come from `get_fills`, not be
    guessed from the placement response.
    """
    if not isinstance(response, dict):
        raise VenuePayloadError("place-order response was not an object", raw=response)
    order_id = str(response.get("orderID") or response.get("order_id") or "")
    success = bool(response.get("success", True))
    status: Literal["open", "filled", "partially_filled", "cancelled", "rejected"] = (
        "open" if success else "rejected"
    )
    return OrderAck(
        venue="polymarket",
        order_id=order_id,
        client_order_id=order.client_order_id,
        status=status,
        filled_size=0.0,
        remaining_size=order.size,
        avg_fill_price=None,
        ts=utcnow(),
    )
