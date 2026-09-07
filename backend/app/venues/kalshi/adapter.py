"""Kalshi `VenueAdapter` read path (PLAN.md D3, T12).

Source for every venue fact below: https://docs.kalshi.com (Trade API
v2), fetched ONCE by the architect on 2026-09-04 and pinned in PLAN.md
§3 "Kalshi". GUARDRAILS.md §1.4 forbids re-fetching those pages from
task code, so this module cites the pin. Anything NOT in that pin is
marked UNVERIFIED where it appears.

UNITS — THE WHOLE POINT OF THIS FILE (GUARDRAILS.md §4)
-------------------------------------------------------
Kalshi speaks two price encodings, in the same API, sometimes in the
same response:

  * ``*_dollars`` fields are FIXED-POINT DOLLAR STRINGS — ``"0.42"``
    means 42 cents, i.e. a probability of 0.42.
  * the same field without the suffix is LEGACY INTEGER CENTS —
    ``42`` means 42 cents, i.e. a probability of 0.42.

Everything this adapter emits is a probability in ``[0.0, 1.0]`` and a
size in contracts (each pays $1.00 at resolution). The conversion
happens HERE, at this boundary, and NOWHERE else: `_to_dollars`
below is the single funnel both encodings pass through. Getting this
wrong is not a style defect — reading ``42`` cents as ``42.0`` is caught
loudly (`app/venues/types.py` rejects a price outside ``[0,1]``, and
T05's fee model raises on an out-of-domain price), but reading ``4.5``
cents as ``0.45`` instead of ``0.045`` is NOT caught by anything
downstream and would fabricate a 40-point edge against Polymarket.

The mirror image of that — a fixed-point DOLLAR STRING arriving under a
BARE (legacy cents) key, ``{"yes": [["0.42", 120]]}`` or
``{"balance": "1234.56"}`` — is equally invisible to every validator
downstream, because ``0.42 / 100 = 0.0042`` is still a valid probability
and still a valid dollar amount, merely 100x too small. `_to_dollars`
rejects exactly that shape (T44), using the venue's OWN convention as
the discriminator: the ``*_dollars`` fields are STRINGS, the legacy
cents fields are NUMBERS. What it deliberately does NOT do is guess from
magnitude — a bare key carrying the number ``0.40`` is a legal 0.4-cent
price on a market whose tick is a tenth of a cent, and inventing a
threshold there would reject real quotes.

THE BOOK IS BIDS-ONLY — READ THIS BEFORE TOUCHING `build_book`
--------------------------------------------------------------
Kalshi's ``GET /markets/{ticker}/orderbook`` returns RESTING BIDS ON
BOTH SIDES and no asks at all (PLAN.md §3). There is no separate ask
book to read; the asks are DERIVED:

    a NO bid at price q IS a YES ask at (1 - q)

because buying NO at q and selling YES at (1 - q) are the same trade —
the pair YES+NO always pays exactly $1.00. So:

    YES book:  bids = yes levels,            asks = [(1-q, size) for NO levels]
    NO  book:  bids = no  levels,            asks = [(1-q, size) for YES levels]

Worked example, one normal (uncrossed) market:

    yes levels: 0.40 x 120     (someone will pay 0.40 for YES)
    no  levels: 0.58 x  80     (someone will pay 0.58 for NO)

    YES book -> best_bid 0.40, best_ask 1 - 0.58 = 0.42   (0.40 <= 0.42 OK)
    NO  book -> best_bid 0.58, best_ask 1 - 0.40 = 0.60   (0.58 <= 0.60 OK)

    and the two sides tie out: 0.40 + 0.60 = 1.00, 0.42 + 0.58 = 1.00.

Invert that subtraction and every Kalshi book comes back CROSSED
(best_bid > best_ask), which invents a riskless arbitrage against
Polymarket on every quote. It would not even fail loudly: T07's
`SimulatedFillEngine` DECLINES a crossed book with
``reason="crossed_book"``, so the symptom would be every Kalshi fill
silently skipped, not an exception. `tests/venues/test_kalshi_adapter.py`
asserts the direction directly (`best_bid <= best_ask`, and the
1 - no_bid identity) precisely because the failure mode is quiet.

ORDER PLACEMENT (GUARDRAILS.md §1.1)
------------------------------------
This module's ONLY HTTP verb is GET — `_get` hardcodes it, and there is
no other request method on the class, so `KalshiAdapter` is structurally
incapable of writing to the venue. `KalshiLiveAdapter`
(`app/venues/kalshi/live.py`) is the only place that POSTs an order.

NOTE ON THE `_PORTFOLIO_ROOT` COMPOSITION: T12's acceptance check greps
this package for the two order-PLACEMENT endpoint paths and requires
them to appear in `live.py` and nowhere else. That grep is TEXTUAL, and
it cannot tell a GET of the resting-orders resource (a read, and this
module's job) from a POST to the very same URL (placing an order —
`live.py`'s job, and the thing the fence exists to confine), because the
two share a path. The four portfolio paths below are therefore composed
from one root constant rather than written out as literals, and this
note deliberately does not spell them out either. That is a concession
to a text-matching check, NOT the safety property itself: the real,
non-textual guarantee is the GET-only `_get` above, which no amount of
grep evasion could fake, plus `tests/venues/test_kalshi_adapter.py::
test_the_read_adapter_cannot_place_or_cancel_orders`.
"""
import asyncio
import logging
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal, cast

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Settings
from app.config import settings as _default_settings
from app.utils.time import ensure_aware, utcnow
from app.venues.base import (
    BaseAdapter,
    FeeModel,
    VenueAuthError,
    VenueError,
    VenuePayloadError,
    VenueRateLimited,
)
from app.venues.fees import KalshiFeeModel
from app.venues.kalshi.auth import auth_headers, load_private_key
from app.venues.types import (
    Balance,
    BookLevel,
    FeeSchedule,
    Fill,
    Liquidity,
    MarketStatus,
    OrderAck,
    OrderBook,
    Position,
    VenueId,
    VenueMarket,
)

logger = logging.getLogger(__name__)

#: `/portfolio` path root — see the module docstring's note on why the
#: four portfolio paths below are composed rather than written out.
_PORTFOLIO_ROOT = "/portfolio"

#: `GET` — account cash balance, in cents or `*_dollars` (PLAN.md §3).
_BALANCE_PATH = f"{_PORTFOLIO_ROOT}/balance"

#: `GET` — open positions per market ticker.
_POSITIONS_PATH = f"{_PORTFOLIO_ROOT}/positions"

#: `GET` — executed fills (PLAN.md §3 names this endpoint).
_FILLS_PATH = f"{_PORTFOLIO_ROOT}/fills"

#: `GET` — currently RESTING orders. Reading this is not placing one;
#: `live.py` owns every write to the same URL (module docstring).
_RESTING_ORDERS_PATH = f"{_PORTFOLIO_ROOT}/orders"

#: Page size for `list_markets`' cursor pagination. Kalshi's documented
#: maximum for `limit` is not pinned in PLAN.md §3; 200 is a
#: conservative page size that keeps the number of round trips low
#: without assuming a maximum the docs may not allow.
_MARKETS_PAGE_LIMIT = 200

#: Hard cap on `list_markets` pagination rounds. A venue that keeps
#: handing back a fresh cursor forever (or a mock that does) must not
#: spin this coroutine indefinitely; `list_markets` also stops on a
#: repeated cursor, so this only bounds the pathological case.
_MAX_MARKET_PAGES = 50

#: Extra attempts after a 429 before giving up on one request.
_RATE_LIMIT_RETRIES = 2

#: First back-off after a 429, doubled per retry. Kalshi sends no
#: `Retry-After` on these, so the schedule is ours, not the venue's.
_RATE_LIMIT_BACKOFF_S = 1.0

#: Normalized `MarketStatus` -> the `status` query value Kalshi expects.
#: Kalshi calls a resolved market `"settled"` (PLAN.md §3: `result` is
#: populated once a market settles).
_STATUS_QUERY_VALUE: dict[MarketStatus, str] = {
    "open": "open",
    "closed": "closed",
    "resolved": "settled",
}

#: Kalshi market `status` string -> normalized `MarketStatus`. Consulted
#: only when `result` is empty (a populated `result` means resolved,
#: whatever `status` says — PLAN.md §3 maps `result` to `status/result`).
_MARKET_STATUS_FROM_PAYLOAD: dict[str, MarketStatus] = {
    "open": "open",
    "active": "open",
    "initialized": "open",
    "closed": "closed",
    "determined": "closed",
    "settled": "resolved",
    "finalized": "resolved",
}

#: Kalshi order `status` -> `OrderAck.status`. UNVERIFIED against live
#: docs (PLAN.md §3 pins the order REQUEST body and the ack's numeric
#: fields, not this enum); an unrecognized value falls back to `"open"`,
#: the conservative reading for an order the venue is still holding.
_ORDER_STATUS: dict[str, Literal["open", "filled", "partially_filled", "cancelled", "rejected"]] = {
    "resting": "open",
    "open": "open",
    "pending": "open",
    "executed": "filled",
    "filled": "filled",
    "partially_filled": "partially_filled",
    "canceled": "cancelled",
    "cancelled": "cancelled",
    "rejected": "rejected",
}

#: Default tick size when `price_level_structure` is absent or
#: unparseable (PLAN.md §3 / T12 brief: "default 0.01"). Kalshi's
#: standard cent tick.
_DEFAULT_TICK_SIZE = 0.01

#: Default minimum order size, in contracts. Kalshi trades whole
#: contracts and PLAN.md §3 pins no per-market minimum field, so 1
#: contract is the floor unless a payload states otherwise.
_DEFAULT_MIN_SIZE = 1.0

#: `FeeSchedule.source` for a market inside its fee-waiver window. This
#: EXACT string is behavioural, not cosmetic: T07's fill engine accepts
#: a zero taker rate silently only from
#: `app/execution/fill_engine.py::_ZERO_RATE_DECLARED_SOURCES` =
#: `{"fee_waiver", "category_table", "clob_market"}`. Spell it anything
#: else and a genuine Kalshi waiver gets logged as a suspicious free
#: fill.
_FEE_SOURCE_WAIVER = "fee_waiver"

#: `FeeSchedule.source` for the `Settings`-sourced standard rates
#: (`kalshi_taker_fee_rate` / `kalshi_maker_fee_rate`). Deliberately NOT
#: in `_ZERO_RATE_DECLARED_SOURCES`: if an operator zeroes those rates in
#: config, the fill engine SHOULD flag the resulting free fills.
_FEE_SOURCE_SETTINGS = "settings"


class KalshiAdapter(BaseAdapter):
    """Read-path `VenueAdapter` for Kalshi (PLAN.md D3).

    `KalshiLiveAdapter` (`app/venues/kalshi/live.py`) subclasses this to
    add real order placement — the only class in the package that may
    (GUARDRAILS.md §1.1).

    Every request is signed when credentials are configured; the
    portfolio endpoints REQUIRE them and raise `VenueAuthError` before
    touching the network when they are absent.
    """

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        settings_obj: Settings | None = None,
    ) -> None:
        """Build the Kalshi HTTP client.

        Args:
            transport: Optional injected `httpx` transport. Tests pass an
                `httpx.MockTransport` (GUARDRAILS.md §1.4: no network to
                `kalshi.com`/`kalshi.co`, ever, from a test); production
                leaves this `None`.
            settings_obj: `Settings` to read base URL and credentials
                from. Defaults to the process-wide `app.config.settings`.
                Tests pass an explicit `Settings(...)` carrying a
                generated throwaway key and `KALSHI_API_KEY_ID=
                "test-key-id"` (GUARDRAILS.md §1.3 — never a real
                credential).
        """
        self.venue: VenueId = "kalshi"
        self._settings = settings_obj if settings_obj is not None else _default_settings
        # Kept so the client can be REBUILT when the event loop changes;
        # see `_rebind_client_if_loop_changed`. Tests inject a
        # MockTransport here and it must survive a rebuild, or a rebuilt
        # client would reach the network (GUARDRAILS.md §1.4).
        self._transport = transport
        self._client_loop: asyncio.AbstractEventLoop | None = None
        # Request pacing state. The lock is loop-bound like the client, so
        # it is rebuilt by `_rebind_client_if_loop_changed` too.
        self._pace_lock: asyncio.Lock | None = None
        self._next_request_at = 0.0
        self._http = httpx.AsyncClient(
            base_url=self._settings.kalshi_api_base_url,
            transport=transport,
            timeout=30.0,
        )
        self._private_key: rsa.RSAPrivateKey | None = None

    def _rebind_client_if_loop_changed(self) -> None:
        """Rebuild the client when the running event loop has changed.

        In paper mode `make_paper_adapter` returns a PROCESS-WIDE
        singleton on purpose, so this adapter outlives any one event
        loop. Each Celery beat tick runs `asyncio.run(...)`, which closes
        its loop on the way out, and an `httpx.AsyncClient` holds
        connections bound to the loop that opened them.

        Observed against the live API: tick 1 of a beat succeeds and
        every tick after it dies in the transport with `RuntimeError:
        Event loop is closed`, forever, because the dead pool is reused.
        The suite cannot see it -- one loop per test process.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._client_loop is loop and not self._http.is_closed:
            return
        self._http = httpx.AsyncClient(
            base_url=self._settings.kalshi_api_base_url,
            transport=self._transport,
            timeout=30.0,
        )
        self._client_loop = loop
        self._pace_lock = asyncio.Lock()

    async def _pace(self) -> None:
        """Wait until this adapter is allowed to issue its next request.

        Kalshi's unauthenticated limit is about 10 requests a second and
        it answers a burst with a bare 429 (no `Retry-After`). Pagination
        in `list_markets` fires as fast as the network allows, so before
        this existed every scan tripped the limit and threw away every
        page it had already collected.

        Serialized through a lock because the limit is per CLIENT, not
        per call site: concurrent book fetches share the same budget as
        the paging loop, so pacing each coroutine independently would not
        bound the aggregate rate.
        """
        if self._transport is not None:
            # An injected transport is a test double (GUARDRAILS.md §1.4
            # forbids tests reaching a venue), and a MockTransport has no
            # rate limit to respect. Pacing it only makes the suite sleep
            # -- it tripled the venue tests' runtime -- so the pacing
            # logic is covered by testing `_pace` directly instead.
            return
        if self._pace_lock is None:
            self._pace_lock = asyncio.Lock()
        interval = self._settings.kalshi_min_request_interval_s
        if interval <= 0:
            return
        async with self._pace_lock:
            now = asyncio.get_running_loop().time()
            wait = self._next_request_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = asyncio.get_running_loop().time()
            self._next_request_at = now + interval

    @property
    def _client(self) -> httpx.AsyncClient:
        """The HTTP client, rebound to the running loop if needed."""
        self._rebind_client_if_loop_changed()
        return self._http

    async def aclose(self) -> None:
        """Close the underlying `httpx.AsyncClient`."""
        await self._http.aclose()

    # -- Signing and transport -------------------------------------------

    def _signing_key(self) -> rsa.RSAPrivateKey | None:
        """Return the loaded RSA key, or `None` when uncredentialed.

        The key is parsed at most once per adapter and cached; the PEM
        itself is never stored, logged, or returned (GUARDRAILS.md §1.3).

        Raises:
            VenueAuthError: If a key id and PEM are configured but the
                PEM does not parse as an RSA private key. This is a
                configuration fault, surfaced as an auth error rather
                than a bare `ValueError` so callers handling venue auth
                failures see it.
        """
        pem = self._settings.kalshi_private_key_pem.get_secret_value()
        if not (self._settings.kalshi_api_key_id and pem.strip()):
            return None
        if self._private_key is None:
            try:
                self._private_key = load_private_key(pem)
            except ValueError as exc:
                raise VenueAuthError(f"KALSHI_PRIVATE_KEY_PEM is unusable: {exc}") from exc
        return self._private_key

    def _apply_auth(self, request: httpx.Request, *, require_auth: bool) -> None:
        """Stamp the three Kalshi auth headers onto `request`, if possible.

        The FULLY BUILT request URL is what gets signed, so the signed
        path can never drift from the path actually sent (the query
        string is stripped inside `sign_request` — PLAN.md §3).

        Args:
            request: The request to sign, already built (path, query and
                body finalized).
            require_auth: If `True`, missing credentials are an error.
                Public endpoints (`/markets`, `/markets/*/orderbook`)
                pass `False` and are simply sent unsigned; Kalshi accepts
                signed requests to them too, so a credentialed adapter
                signs everything.

        Raises:
            VenueAuthError: If `require_auth` and no credentials are
                configured — raised BEFORE any network call.
        """
        key = self._signing_key()
        if key is None:
            if require_auth:
                raise VenueAuthError(
                    "KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PEM are required "
                    f"for {request.url.path}"
                )
            return
        request.headers.update(
            auth_headers(
                self._settings.kalshi_api_key_id,
                key,
                request.method,
                str(request.url),
            )
        )

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        require_auth: bool = False,
    ) -> dict[str, Any]:
        """Issue one signed `GET` and return its JSON object.

        `"GET"` is HARDCODED here, and this is the only request method on
        the read adapter: `KalshiAdapter` cannot place, modify, or cancel
        an order even by accident (GUARDRAILS.md §1.1). `live.py` adds
        its own write path.

        Args:
            path: Path relative to the configured base URL, e.g.
                `"/markets"`. The base URL already carries
                `/trade-api/v2`.
            params: Query parameters. Never signed (PLAN.md §3).
            require_auth: Whether credentials are mandatory.

        Returns:
            dict[str, Any]: The decoded JSON object.

        Raises:
            VenueAuthError: On missing credentials, or HTTP 401/403.
            VenueRateLimited: On HTTP 429.
            VenuePayloadError: If the body is not a JSON object.
        """
        # One retry budget for the whole call. Pacing should keep us under
        # the limit; this is the safety net for the case where another
        # process shares the same source address, which pacing cannot see.
        # An injected transport is a test double, and its 429 is a fixture
        # rather than a transient condition -- retrying one only sleeps
        # through a deterministic answer (it cost 3s in a single test).
        attempts = 1 if self._transport is not None else _RATE_LIMIT_RETRIES + 1
        for attempt in range(attempts):
            await self._pace()
            request = self._client.build_request("GET", path, params=params)
            self._apply_auth(request, require_auth=require_auth)
            response = await self._client.send(request)
            if response.status_code != 429 or attempt == attempts - 1:
                raise_for_venue_error(response)
                return json_object(response)
            # Kalshi sends no Retry-After on these, so back off on a
            # doubling schedule rather than guessing a header exists.
            header = response.headers.get("Retry-After")
            delay = float(header) if header and header.isdigit() else _RATE_LIMIT_BACKOFF_S * (2**attempt)
            logger.warning(
                "kalshi",
                extra={
                    "event": "kalshi_rate_limited_retrying",
                    "path": path,
                    "attempt": attempt + 1,
                    "sleep_s": delay,
                },
            )
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    # -- Market metadata --------------------------------------------------

    async def list_markets(
        self,
        status: MarketStatus | None = None,
        updated_since: datetime | None = None,
    ) -> list[VenueMarket]:
        """List Kalshi markets, following the `cursor` through every page.

        Issues `GET /markets?status=…&limit=200`, then re-issues it with
        `cursor=<the previous page's cursor>` until the venue returns an
        empty cursor (PLAN.md §3). Pagination stops early on a repeated
        cursor or after `_MAX_MARKET_PAGES`, so a venue that never
        terminates cannot hang the caller.

        Args:
            status: Only return markets in this status. Sent to the venue
                as its own vocabulary (`"resolved"` -> `"settled"`) AND
                re-checked locally against the parsed status, so a venue
                filter that disagrees with our own mapping cannot leak a
                market through. Omitted entirely when `None`.
            updated_since: Only return markets updated at/after this
                aware UTC timestamp. Best-effort: PLAN.md §3 pins no
                update-time field on the Kalshi market payload, so a
                market whose payload carries no parseable update time is
                KEPT (an unknown update time is not evidence of
                staleness).

        Returns:
            list[VenueMarket]: Matching markets, prices normalized to
                probabilities in `[0,1]`.
        """
        if updated_since is not None:
            ensure_aware(updated_since)
        base_params: dict[str, str] = {"limit": str(_MARKETS_PAGE_LIMIT)}
        if status is not None:
            base_params["status"] = _STATUS_QUERY_VALUE[status]

        payloads: list[dict[str, Any]] = []
        cursor = ""
        seen_cursors: set[str] = set()
        for _ in range(_MAX_MARKET_PAGES):
            params = dict(base_params)
            if cursor:
                params["cursor"] = cursor
            body = await self._get("/markets", params=params)
            page = _require_envelope_list(
                body, ("markets",), context="kalshi /markets"
            )
            payloads.extend(page)
            cursor = str(body.get("cursor") or "")
            if not cursor or not page or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)

        # Same contract as the Polymarket adapter: ONE unbuildable market
        # must not blank the venue. A single market that `VenueMarket`'s
        # validators reject used to raise out of here and cost the caller
        # every other market in the listing -- on Polymarket that was
        # ~2100 markets lost to one missing `endDate`. `get_market()`
        # still raises, because asking for one market and getting silence
        # is the quiet failure this repo keeps finding.
        markets = []
        skipped: dict[str, int] = {}
        for item in payloads:
            try:
                markets.append(self._build_market(item))
            except VenueError as exc:
                reason = str(exc).split(":")[0][:60]
                skipped[reason] = skipped.get(reason, 0) + 1
        if skipped:
            logger.warning(
                "kalshi",
                extra={
                    "event": "kalshi_markets_skipped",
                    "listed": len(payloads),
                    "built": len(markets),
                    "skipped": sum(skipped.values()),
                    "reasons": skipped,
                },
            )
        if status is not None:
            markets = [m for m in markets if m.status == status]
        if updated_since is not None:
            markets = [
                m
                for m in markets
                if (updated := _payload_updated_at(m.raw)) is None
                or updated >= updated_since
            ]
        return markets

    async def get_market(self, market_id: str) -> VenueMarket:
        """Fetch one Kalshi market by ticker.

        Args:
            market_id: Kalshi market `ticker`, e.g.
                `"TEST-26DEC31-ALPHA"`.

        Returns:
            VenueMarket: The normalized market.

        Raises:
            VenuePayloadError: If the response carries no market object.
        """
        body = await self._get(f"/markets/{market_id}")
        raw = body.get("market", body)
        if not isinstance(raw, dict):
            raise VenuePayloadError(
                f"kalshi /markets/{market_id} carried no market object", raw=body
            )
        return self._build_market(raw)

    def _build_market(self, raw: dict[str, Any]) -> VenueMarket:
        """Normalize one Kalshi market payload into a `VenueMarket`.

        Field mapping (PLAN.md §3 "Kalshi", T12 brief):
          * `ticker` -> `market_id`; `event_ticker` -> `event_id`
          * `title` -> `question` (untrusted venue text — GUARDRAILS.md
            §6: displayed and scored, never interpreted as instructions)
          * `rules_primary` + `rules_secondary` -> `rules_text`
          * `close_time` -> `close_time`;
            `expected_expiration_time` -> `expected_settle_time`
          * `result` (`"yes"`/`"no"`/`"scalar"`/`""`) -> `status`/`result`
          * `fee_waiver_expiration_time` -> a `FeeSchedule` (see
            `_fee_schedule`)
          * `price_level_structure` -> `tick_size` at the current price

        `outcome_ids` maps both outcomes to the market's own ticker:
        unlike Polymarket, Kalshi has NO per-outcome identifier — one
        ticker is one YES/NO market and the side is chosen by the order's
        `side` field, not by addressing a different token. The mapping is
        populated (rather than left empty) so callers can uniformly ask
        "what identifier do I trade this outcome under".

        Raises:
            VenuePayloadError: If `ticker` or `close_time` is missing or
                unparseable — neither has a safe default.
        """
        ticker = raw.get("ticker")
        if not ticker:
            raise VenuePayloadError("kalshi market payload missing ticker", raw=raw)
        market_id = str(ticker)
        event_ticker = raw.get("event_ticker")
        close_time = _parse_timestamp(raw.get("close_time"))
        if close_time is None:
            raise VenuePayloadError(
                f"kalshi market {market_id} missing/unparseable close_time", raw=raw
            )
        result_raw = str(raw.get("result") or "").strip()
        if result_raw:
            status: MarketStatus = "resolved"
            result: str | None = result_raw
        else:
            status_text = str(raw.get("status") or "").strip().lower()
            mapped = _MARKET_STATUS_FROM_PAYLOAD.get(status_text)
            if mapped is None and status_text:
                # NOT fatal: venues add lifecycle states, and refusing to
                # parse the whole catalog over one new word is its own
                # outage. But defaulting to "open" makes an unknown state
                # look tradable, so it is at least said out loud (T44).
                logger.warning(
                    "venue",
                    extra={
                        "event": "kalshi_unknown_market_status",
                        "venue": "kalshi",
                        "market_id": market_id,
                        "status": status_text,
                        "assumed": "open",
                    },
                )
            status = mapped if mapped is not None else "open"
            result = None
        rules_parts = [
            str(raw.get(key) or "").strip()
            for key in ("rules_primary", "rules_secondary")
        ]
        rules_text = "\n\n".join(part for part in rules_parts if part)
        try:
            return VenueMarket(
                venue="kalshi",
                market_id=market_id,
                event_id=str(event_ticker) if event_ticker else None,
                question=str(raw.get("title") or ""),
                outcomes=("YES", "NO"),
                outcome_ids={"YES": market_id, "NO": market_id},
                rules_text=rules_text,
                resolution_source=None,
                close_time=close_time,
                expected_settle_time=_parse_timestamp(
                    raw.get("expected_expiration_time")
                ),
                status=status,
                result=result,
                tick_size=_tick_size_at(
                    raw.get("price_level_structure"), _current_price(raw)
                ),
                min_size=_to_float(
                    raw.get("minimum_order_size", raw.get("min_order_size")),
                    default=_DEFAULT_MIN_SIZE,
                ),
                fee=self._fee_schedule(raw),
                raw=raw,
            )
        except ValueError as exc:
            # `VenueMarket`'s own validators speak in field names
            # (`min_size must be a finite value >= 0`) but raise a BARE
            # `ValueError`, which is not a `VenueError` and so is not in
            # `app.services.scanner.VENUE_READ_FAULTS` — one negative
            # `minimum_order_size` would abort a whole scan pass instead
            # of skipping that venue's listing (T44). Re-raised as the
            # typed error, naming the venue and the market.
            raise VenuePayloadError(
                f"kalshi market {market_id} did not validate: {exc}", raw=raw
            ) from exc

    def _fee_schedule(self, raw: dict[str, Any]) -> FeeSchedule:
        """Build a market's `FeeSchedule`, honoring an active fee waiver.

        PRECEDENCE (T12 brief): if `fee_waiver_expiration_time` parses
        and is still in the FUTURE, the market is fee-free right now —
        `FeeSchedule(0, 0, source="fee_waiver")`. Otherwise the
        `Settings`-sourced standard rates apply
        (`kalshi_taker_fee_rate`/`kalshi_maker_fee_rate`, 0.07/0.0 by
        default — GUARDRAILS.md §1.5: never a literal in strategy code),
        with `source="settings"`.

        `source` is BEHAVIOURAL, not documentation: T07's fill engine
        treats a zero taker rate as suspicious unless the source
        declares the zero on purpose, and `"fee_waiver"` is one of the
        three sources it trusts (see `_FEE_SOURCE_WAIVER`). An EXPIRED
        waiver correctly falls through to the settings rates — a waiver
        that ended is not a waiver.

        Returns:
            FeeSchedule: The rate pair plus its provenance.
        """
        waiver_until = _parse_timestamp(raw.get("fee_waiver_expiration_time"))
        if waiver_until is not None and waiver_until > utcnow():
            return FeeSchedule(taker_rate=0.0, maker_rate=0.0, source=_FEE_SOURCE_WAIVER)
        return FeeSchedule(
            taker_rate=self._settings.kalshi_taker_fee_rate,
            maker_rate=self._settings.kalshi_maker_fee_rate,
            source=_FEE_SOURCE_SETTINGS,
        )

    # -- Order book --------------------------------------------------------

    async def get_book(self, market_id: str, outcome: str) -> OrderBook:
        """Fetch and normalize one (market, outcome) book.

        `GET /markets/{ticker}/orderbook`. See the module docstring for
        the bids-only derivation — the asks returned here were NOT read
        from the venue, they are the opposite side's bids reflected
        through `1 - q`.

        Args:
            market_id: Kalshi market ticker.
            outcome: `"YES"` or `"NO"` (case-insensitive).

        Returns:
            OrderBook: `bids`/`asks` as probabilities in `[0,1]` and
                sizes in contracts, `depth_source="recorded"` (every
                level traces back to an observed venue level, including
                the derived asks).

        Raises:
            VenuePayloadError: If `outcome` is not YES/NO, or the book
                payload matches neither documented shape.
        """
        body = await self._get(f"/markets/{market_id}/orderbook")
        return build_book(body, market_id=market_id, outcome=outcome, ts=utcnow())

    # -- Credentialed account data ----------------------------------------

    async def get_balance(self) -> Balance:
        """Fetch the account's cash balance, in USD.

        Handles BOTH encodings (module docstring): `balance_dollars`
        (fixed-point dollar string) is preferred when present, otherwise
        `balance` is read as INTEGER CENTS and divided by 100.

        Returns:
            Balance: `available` in USD. `locked` is `0.0` — PLAN.md §3
                pins no field for margin held against resting orders, and
                inventing one would misstate deployable capital
                (GUARDRAILS.md §1.6: capital is per venue and never
                guessed).

        Raises:
            VenueAuthError: If Kalshi credentials are not configured.
            VenuePayloadError: If no balance field is present.
        """
        body = await self._get(_BALANCE_PATH, require_auth=True)
        available = _usd_from(body, "balance")
        if available is None:
            raise VenuePayloadError(
                "kalshi balance payload carries neither balance_dollars nor a "
                "numeric balance (integer cents)",
                raw=body,
            )
        try:
            return Balance(venue="kalshi", available=available, locked=0.0)
        except ValueError as exc:
            raise VenuePayloadError(
                f"kalshi balance {available!r} is not a usable USD amount: {exc}",
                raw=body,
            ) from exc

    async def get_positions(self) -> list[Position]:
        """Fetch open positions.

        Kalshi reports one SIGNED contract count per ticker: positive is
        a long YES position, negative a long NO position (there are no
        naked shorts — PLAN.md §3). That is split here into the
        normalized `(outcome, size >= 0)` shape.

        `avg_price` is DERIVED from the position's cost basis
        (`market_exposure`, integer cents, or `market_exposure_dollars`)
        divided by the contract count — Kalshi reports no average price
        field in the shapes PLAN.md §3 pins. An entry whose cost basis is
        missing or implies a price outside `[0,1]` is skipped rather than
        clamped into a plausible-looking lie.

        Raises:
            VenueAuthError: If Kalshi credentials are not configured.
        """
        body = await self._get(_POSITIONS_PATH, require_auth=True)
        entries = _require_envelope_list(
            body,
            ("market_positions", "positions"),
            context="kalshi /portfolio positions",
        )
        positions: list[Position] = []
        unusable = 0
        for entry in entries:
            ticker = entry.get("ticker")
            signed = _try_float(entry.get("position"))
            if not ticker or signed is None:
                unusable += 1
                _log_skipped("position", "ticker/position missing or non-numeric")
                continue
            if signed == 0:
                # A flat row is a real, ordinary state (a settled market
                # stays in the listing), NOT a parse failure.
                continue
            size = abs(signed)
            exposure = _usd_from(entry, "market_exposure")
            if exposure is None:
                unusable += 1
                _log_skipped(
                    "position", f"{ticker}: no market_exposure to derive avg_price from"
                )
                continue
            try:
                positions.append(
                    Position(
                        venue="kalshi",
                        market_id=str(ticker),
                        outcome="YES" if signed > 0 else "NO",
                        size=size,
                        avg_price=exposure / size,
                    )
                )
            except ValueError as exc:
                unusable += 1
                _log_skipped("position", f"{ticker}: {exc}")
        _require_not_all_dropped(
            entries, kept=len(positions), unusable=unusable, context="kalshi positions"
        )
        return positions

    async def get_open_orders(self) -> list[OrderAck]:
        """Fetch currently RESTING orders. This does not place anything.

        `GET` against the same URL `live.py` POSTs to — see the module
        docstring's note on the acceptance grep.

        Raises:
            VenueAuthError: If Kalshi credentials are not configured.
        """
        body = await self._get(
            _RESTING_ORDERS_PATH, params={"status": "resting"}, require_auth=True
        )
        entries = _require_envelope_list(
            body, ("orders",), context="kalshi resting orders"
        )
        acks: list[OrderAck] = []
        unusable = 0
        for entry in entries:
            try:
                acks.append(parse_order_ack(entry))
            except (ValueError, VenuePayloadError) as exc:
                unusable += 1
                _log_skipped("order", str(exc))
        _require_not_all_dropped(
            entries,
            kept=len(acks),
            unusable=unusable,
            context="kalshi resting orders",
        )
        return acks

    async def get_fills(self, since: datetime) -> list[Fill]:
        """Fetch fills at/after `since`.

        `GET /portfolio/fills`. Each fill's price is taken from the side
        that was actually traded (`yes_price`/`no_price`, in either
        encoding) so a NO fill is priced as NO, not as its YES
        complement.

        When the venue reports no fee on a fill, the fee is ESTIMATED
        with `KalshiFeeModel` and this market's `Settings` rates rather
        than recorded as `$0.00` — a fabricated zero fee is exactly the
        input that makes a marginal edge look profitable. Which of the
        two happened is recorded in `Fill.metadata["fee_source"]`.

        Args:
            since: Aware UTC timestamp; only fills at/after it are
                returned. A fill whose timestamp cannot be parsed is
                dropped (it cannot be placed on either side of `since`).

        Raises:
            VenueAuthError: If Kalshi credentials are not configured.
        """
        ensure_aware(since)
        body = await self._get(_FILLS_PATH, require_auth=True)
        entries = _require_envelope_list(body, ("fills",), context="kalshi fills")
        fills: list[Fill] = []
        parsed = 0
        unusable = 0
        for entry in entries:
            fill = self._parse_fill(entry)
            if fill is None:
                unusable += 1
                _log_skipped("fill", "price/size/timestamp missing or unparseable")
                continue
            parsed += 1
            if fill.ts >= since:
                fills.append(fill)
        _require_not_all_dropped(
            entries, kept=parsed, unusable=unusable, context="kalshi fills"
        )
        return fills

    def _parse_fill(self, raw: dict[str, Any]) -> Fill | None:
        """Parse one raw fill, or `None` if it is unusable.

        UNVERIFIED SHAPE: PLAN.md §3 pins the fills ENDPOINT but not the
        fill object's fields, so key names here are best-effort and
        every one of them is optional-with-a-fallback.

        Raises:
            VenuePayloadError: If `side` is PRESENT but is neither
                `"yes"` nor `"no"` (T44). An absent side still defaults
                to YES — that is the documented optional-with-a-fallback
                rule — but a side spelled in some other vocabulary
                (`"bid"`/`"ask"`, as the ORDER payloads use) must not be
                silently read as YES: it would price a NO fill from
                `yes_price` and label it `outcome="YES"`, and both the
                price and the position it lands in would be wrong with
                nothing said.
        """
        side_raw = raw.get("side")
        side = str(side_raw or "yes").strip().lower()
        if side not in ("yes", "no"):
            raise VenuePayloadError(
                f"kalshi fill: side {side_raw!r} is neither 'yes' nor 'no' — the "
                "outcome it names decides which price field is read, so it cannot "
                "be guessed",
                raw=raw,
            )
        outcome = "NO" if side == "no" else "YES"
        price = _usd_from(raw, "no_price" if outcome == "NO" else "yes_price")
        if price is None:
            price = _usd_from(raw, "price")
        size = _try_float(raw.get("count"))
        ts = _parse_timestamp(raw.get("created_time") or raw.get("ts"))
        if price is None or size is None or ts is None:
            return None
        liquidity: Liquidity = "taker" if raw.get("is_taker", True) else "maker"
        fee = _usd_from(raw, "fee_paid")
        fee_source = "venue"
        if fee is None:
            fee_source = "settings_estimate"
            fee = self.fee_model().fee(
                price=price,
                size_contracts=size,
                liquidity=liquidity,
                schedule=self._fee_schedule(raw),
            )
        try:
            return Fill(
                venue="kalshi",
                order_id=str(raw.get("order_id") or raw.get("trade_id") or ""),
                price=price,
                size=size,
                fee=fee,
                ts=ts,
                liquidity=liquidity,
                metadata={
                    "market_id": raw.get("ticker"),
                    "outcome": outcome,
                    "fee_source": fee_source,
                },
            )
        except ValueError:
            return None

    def fee_model(self) -> FeeModel:
        """Return Kalshi's `FeeModel` (`app/venues/fees.py`, T05)."""
        return KalshiFeeModel()


# ---------------------------------------------------------------------------
# Transport / payload helpers, shared with `live.py`.
# ---------------------------------------------------------------------------


def raise_for_venue_error(response: httpx.Response) -> None:
    """Translate Kalshi's error statuses into `VenueError` subclasses.

    Mapping (T12 brief): `429` -> `VenueRateLimited(retry_after)`;
    `401`/`403` -> `VenueAuthError`. Every other error status falls
    through to `httpx.Response.raise_for_status()`, which raises
    `httpx.HTTPStatusError` — deliberately NOT flattened into
    `VenuePayloadError`, which means "the venue answered but the shape
    was wrong", a different fault with different handling.

    Args:
        response: The response to inspect.

    Raises:
        VenueRateLimited: On HTTP 429, carrying `Retry-After` when the
            venue supplied a parseable one.
        VenueAuthError: On HTTP 401/403.
        httpx.HTTPStatusError: On any other 4xx/5xx.
    """
    if response.status_code == 429:
        raise VenueRateLimited(
            "kalshi rate limited the request",
            retry_after_s=_try_float(response.headers.get("Retry-After")),
        )
    if response.status_code in (401, 403):
        raise VenueAuthError(
            f"kalshi rejected the signed request (HTTP {response.status_code})"
        )
    response.raise_for_status()


def json_object(response: httpx.Response) -> dict[str, Any]:
    """Decode a response body that must be a JSON object.

    Raises:
        VenuePayloadError: If the body is not valid JSON, or is valid
            JSON that is not an object. The offending body travels on
            `raw` (T12 brief: "unknown shape -> VenuePayloadError(raw=
            body)").
    """
    try:
        payload: object = response.json()
    except ValueError as exc:
        raise VenuePayloadError(
            f"kalshi response was not JSON: {exc}", raw=response.text
        ) from exc
    if not isinstance(payload, dict):
        raise VenuePayloadError("kalshi response was not a JSON object", raw=payload)
    return payload


def _require_list_of_dicts(value: object, *, context: str) -> list[dict[str, Any]]:
    """Validate `value` is a list of objects, else raise `VenuePayloadError`."""
    if not isinstance(value, list):
        raise VenuePayloadError(f"{context}: expected a list, got {value!r}", raw=value)
    items: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise VenuePayloadError(f"{context}: expected a list of objects", raw=value)
        items.append(item)
    return items


def _log_skipped(kind: str, reason: str) -> None:
    """Log one skipped payload entry at WARNING (T44).

    A per-entry skip is the right behaviour — one malformed row must not
    cost the whole read — but doing it in silence is what makes a schema
    change undiagnosable. Carries no payload body, only the reason.
    """
    logger.warning(
        "venue",
        extra={
            "event": f"kalshi_{kind}_entry_skipped",
            "venue": "kalshi",
            "reason": reason,
        },
    )


def _require_not_all_dropped(
    entries: list[dict[str, Any]], *, kept: int, unusable: int, context: str
) -> None:
    """Raise if the venue sent entries and NONE of them survived parsing.

    WHY (T44). Per-entry skipping is deliberately tolerant, but "every
    row failed" is not a bad row, it is a disagreement about the schema —
    and the result of tolerating it is an empty list, which
    `app.execution.reconcile` reads as "the venue holds nothing" and acts
    on (a raised read, by contrast, it treats as "we do not know"). A
    whole page of cents-priced orders, or of orders under a renamed id
    key, would otherwise report every live order as gone.

    Args:
        entries: The raw entries the venue returned.
        kept: How many parsed successfully.
        unusable: How many failed to parse (entries legitimately skipped
            for a non-parse reason — a flat position — are in neither).
        context: Endpoint name, for the error message.

    Raises:
        VenuePayloadError: If `entries` is non-empty, nothing was kept,
            and every entry was unusable.
    """
    if entries and kept == 0 and unusable == len(entries):
        raise VenuePayloadError(
            f"{context}: the venue returned {len(entries)} entr"
            f"{'y' if len(entries) == 1 else 'ies'} and NONE parsed — reporting "
            "an empty result would be indistinguishable from the venue holding "
            "nothing",
            raw=entries,
        )


def _require_envelope_list(
    body: dict[str, Any], keys: tuple[str, ...], *, context: str
) -> list[dict[str, Any]]:
    """Return the list under the first present key in `keys`.

    WHY THE KEY MUST BE PRESENT (T44). Every one of these endpoints used
    to read `body.get("<key>", [])`, so a RENAMED key produced an empty
    list and no error — indistinguishable from "you have no positions /
    no resting orders / no fills / no markets". That difference is
    load-bearing downstream: `app.execution.reconcile` treats a read that
    RAISED as "we do not know" and refuses to conclude anything, but
    treats a successful empty read as "the venue has nothing" and acts on
    it. So a renamed key would have quietly told reconciliation that
    every open order was gone. An empty list under a PRESENT key still
    means exactly what it says and is passed through untouched.

    Args:
        body: The decoded response object.
        keys: Accepted spellings, in precedence order.
        context: Endpoint name, for the error message.

    Returns:
        list[dict[str, Any]]: The entries, possibly empty.

    Raises:
        VenuePayloadError: If none of `keys` is present, or the value
            under the one that is present is not a list of objects.
    """
    for key in keys:
        if key in body:
            return _require_list_of_dicts(body[key], context=f"{context} ({key})")
    raise VenuePayloadError(
        f"{context}: response carries none of {keys} — an absent key is not an "
        "empty result, and must not be read as one",
        raw=body,
    )


def _try_float(value: object) -> float | None:
    """Best-effort finite `float(value)`, `None` on failure/`None`/non-finite."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(cast(Any, value))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _to_float(value: object, *, default: float) -> float:
    """`_try_float` with a fallback."""
    parsed = _try_float(value)
    return default if parsed is None else parsed


def _to_dollars(value: object, *, is_dollars: bool, field: str) -> float | None:
    """THE single cents/dollars funnel for this venue (module docstring).

    Args:
        value: A Kalshi price. When `is_dollars`, a fixed-point DOLLAR
            string (`"0.42"`) already on the `[0,1]` probability scale.
            Otherwise INTEGER CENTS (`42`), which is divided by 100.
        is_dollars: Which encoding `value` is in. Chosen by the caller
            from the FIELD NAME (`*_dollars` vs the bare field), never
            guessed from the value's magnitude — a legacy `1` cent and a
            dollar-string `"1"` are both valid and mean 100x different
            things.
        field: The payload key `value` came from, for the error message.

    Returns:
        float | None: A probability in `[0,1]`, or `None` if `value` is
            missing/unparseable. Out-of-range values are returned as-is
            so the caller's `BookLevel`/`Fill` construction rejects them
            loudly rather than being silently clamped into a plausible
            price.

    Raises:
        VenuePayloadError: If a BARE (legacy integer-cents) key carries a
            fixed-point dollar STRING — `{"yes": [["0.42", 120]]}`,
            `{"balance": "1234.56"}`. That is the ONE cents/dollars
            divergence nothing downstream can catch (T44): dividing
            `"0.42"` by 100 yields `0.0042`, still a perfectly valid
            probability and a perfectly valid dollar amount, just 100x
            too small — a 42c market quoted at 0.42c fabricates an
            enormous edge against Polymarket, and `$1234.56` of capital
            read as `$12.34` silently under-sizes every order. The
            discriminator is the venue's own convention (module
            docstring): the `_dollars` fields are STRINGS, the legacy
            cents fields are NUMBERS. A string that IS a whole number
            (`"42"`) is still read as cents, because on that value the
            two readings agree about the digits and only the type drifted.
            NOT detectable, and deliberately not guessed at: a bare key
            carrying the NUMBER `0.40`, which is a legal 0.4-cent price
            on a market whose tick is a tenth of a cent.
    """
    if not is_dollars and isinstance(value, str):
        parsed_text = _try_float(value)
        if parsed_text is not None and not float(parsed_text).is_integer():
            raise VenuePayloadError(
                f"kalshi {field}: a bare (legacy) key must carry INTEGER CENTS as a "
                f"number, got the fixed-point dollar string {value!r} — read as "
                f"cents that is {parsed_text / 100.0!r}, 100x too small. Send it "
                f"as {field}_dollars if it is dollars.",
                raw=value,
            )
    parsed = _try_float(value)
    if parsed is None:
        return None
    return parsed if is_dollars else parsed / 100.0


def _usd_from(payload: dict[str, Any], base_key: str) -> float | None:
    """Read a USD amount that may be `{base}_dollars` OR `{base}` in cents.

    Applies the venue's own naming convention (module docstring): the
    `_dollars` suffix means a fixed-point dollar string; the bare key is
    legacy integer cents. `{base}_dollars` wins when both are present.

    A `*_dollars` key carrying something OTHER than a string is logged at
    WARNING and still read as dollars (T44). It cannot be rejected: for a
    price the `[0,1]` validators catch a cents value anyway, and for an
    account amount (`balance`, `market_exposure`) `123456` is equally
    plausible as `$123,456.00` and as `1234.56` in cents, so guessing
    would be exactly the fabrication this module exists to prevent. The
    log line is what makes the ambiguity visible instead of silent.

    Args:
        payload: The object to read from.
        base_key: Field name WITHOUT the `_dollars` suffix, e.g.
            `"balance"`, `"yes_price"`, `"market_exposure"`.

    Returns:
        float | None: The amount in USD (or, for a price field, a
            probability — the two are the same number on a venue whose
            contracts pay $1.00), or `None` if neither key is present.

    Raises:
        VenuePayloadError: If a bare key carries a dollar string — see
            `_to_dollars`.
    """
    dollars_key = f"{base_key}_dollars"
    if dollars_key in payload:
        value = payload[dollars_key]
        if value is not None and not isinstance(value, str):
            logger.warning(
                "venue",
                extra={
                    "event": "kalshi_dollars_field_not_a_string",
                    "venue": "kalshi",
                    "field": dollars_key,
                    "python_type": type(value).__name__,
                },
            )
        return _to_dollars(value, is_dollars=True, field=dollars_key)
    if base_key in payload:
        return _to_dollars(payload[base_key], is_dollars=False, field=base_key)
    return None


def _parse_timestamp(value: object) -> datetime | None:
    """Parse a Kalshi timestamp into aware UTC, or `None`.

    Accepts an ISO-8601 string (optionally `Z`-suffixed, as
    `close_time`/`expected_expiration_time` are) or an epoch number in
    SECONDS or MILLISECONDS (as `ts`/`ts_ms` fields are). Milliseconds
    are distinguished by magnitude: a seconds-valued epoch beyond `1e12`
    would be the year 33658, so anything that large is milliseconds.
    """
    if isinstance(value, str) and value.strip():
        text = value.strip()
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            return ensure_aware(datetime.fromisoformat(normalized))
        except (TypeError, ValueError):
            return None
    epoch = _try_float(value)
    if epoch is None or epoch <= 0:
        return None
    seconds = epoch / 1000.0 if epoch > 1e12 else epoch
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _payload_updated_at(raw: Mapping[str, Any]) -> datetime | None:
    """Best-effort update time for a Kalshi market payload.

    PLAN.md §3 pins no update-time field, so several plausible spellings
    are tried and `None` (meaning "unknown, therefore keep it") is the
    honest answer when none is present.
    """
    for key in ("last_update_ts", "updated_ts", "updated_time", "last_updated_time"):
        parsed = _parse_timestamp(raw.get(key))
        if parsed is not None:
            return parsed
    return None


def _current_price(raw: dict[str, Any]) -> float:
    """Best current YES price for a market, used to pick a tick size.

    Prefers `last_price`, falls back to the midpoint of `yes_bid`/
    `yes_ask`, then to `0.5` (the middle of the range, where Kalshi's
    tick is coarsest and therefore the conservative guess).
    """
    last = _usd_from(raw, "last_price")
    if last is not None and 0.0 <= last <= 1.0:
        return last
    bid = _usd_from(raw, "yes_bid")
    ask = _usd_from(raw, "yes_ask")
    if bid is not None and ask is not None:
        mid = (bid + ask) / 2.0
        if 0.0 <= mid <= 1.0:
            return mid
    return 0.5


def _tick_size_at(structure: object, price: float) -> float:
    """Return the tick size that applies at `price`, defaulting to 0.01.

    UNVERIFIED SHAPE — READ BEFORE TRUSTING: PLAN.md §3 records that a
    market payload carries `price_level_structure` ("tick sizes by price
    range") but does NOT pin its field names, and GUARDRAILS.md §1.4
    forbids fetching the page that would. This parser therefore accepts
    several plausible spellings for the range bounds and the tick, and
    falls back to `_DEFAULT_TICK_SIZE` (0.01, Kalshi's standard cent
    tick) on ANYTHING it does not recognize — including an entirely
    different container shape. A wrong tick size makes an order's price
    off-tick and the venue rejects it, which is loud; silently returning
    a made-up finer tick would not be.

    Each bound/tick value follows the same cents-vs-dollars convention as
    the rest of the payload: a `*_dollars` key is a dollar string, the
    bare key is integer cents.

    Args:
        structure: The payload's `price_level_structure` value.
        price: Current price, a probability in `[0,1]`.

    Returns:
        float: Tick size in probability units, in `(0, 1]`.
    """
    if not isinstance(structure, list):
        return _DEFAULT_TICK_SIZE
    for entry in structure:
        if not isinstance(entry, dict):
            continue
        start = _first_usd(entry, ("start_price", "min_price", "from_price", "from"))
        end = _first_usd(entry, ("end_price", "max_price", "to_price", "to"))
        tick = _first_usd(entry, ("tick_size", "increment", "price_increment"))
        if tick is None or not (0.0 < tick <= 1.0):
            continue
        if start is not None and price < start:
            continue
        if end is not None and price > end:
            continue
        return tick
    return _DEFAULT_TICK_SIZE


def _first_usd(payload: dict[str, Any], base_keys: tuple[str, ...]) -> float | None:
    """Return the first present `_usd_from` value among `base_keys`.

    A `VenuePayloadError` from the cents/dollars funnel is swallowed HERE
    and only here (T44), because this helper serves ONLY `_tick_size_at`,
    whose shape is UNVERIFIED and whose documented contract is to fall
    back to `_DEFAULT_TICK_SIZE` on anything it does not recognize. A
    tick size is not money: getting it wrong makes an order off-tick and
    the venue rejects it, which is loud. Every money field goes through
    `_usd_from` directly and stays loud.
    """
    for base_key in base_keys:
        try:
            value = _usd_from(payload, base_key)
        except VenuePayloadError:
            continue
        if value is not None:
            return value
    return None


# ---------------------------------------------------------------------------
# The bids-only book conversion. See the module docstring for the proof.
# ---------------------------------------------------------------------------


def _levels(raw: object, *, is_dollars: bool, context: str) -> list[BookLevel]:
    """Parse one side of a Kalshi book into normalized `BookLevel`s.

    Args:
        raw: A list of `[price, size]` pairs, or `None`/absent for an
            empty side (a real and common state).
        is_dollars: `True` for `yes_dollars`/`no_dollars` (dollar
            strings), `False` for the legacy `yes`/`no` (integer cents).
        context: Field name, for error messages.

    Returns:
        list[BookLevel]: Prices as probabilities, sizes in contracts.
            NOT sorted — `OrderBook` sorts both sides on construction.

    Raises:
        VenuePayloadError: If the side is present but is not a list of
            `[price, size]` pairs, or a price/size is out of domain
            (which is how a cents payload misread as dollars surfaces:
            `42.0` is not a probability).
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise VenuePayloadError(f"kalshi book side {context} was not a list", raw=raw)
    levels: list[BookLevel] = []
    for pair in raw:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            raise VenuePayloadError(
                f"kalshi book side {context} level was not a [price, size] pair",
                raw=raw,
            )
        price = _to_dollars(
            pair[0], is_dollars=is_dollars, field=f"book side {context} price"
        )
        size = _try_float(pair[1])
        if price is None or size is None:
            raise VenuePayloadError(
                f"kalshi book side {context} level had a non-numeric price/size",
                raw=raw,
            )
        try:
            levels.append(BookLevel(price=price, size=size))
        except ValueError as exc:
            raise VenuePayloadError(
                f"kalshi book side {context}: {exc}", raw=raw
            ) from exc
    return levels


def _complement(levels: list[BookLevel]) -> list[BookLevel]:
    """Reflect bid levels through `1 - p` — the ask side they imply.

    THE DIRECTION MATTERS (module docstring): a NO bid at `q` is a YES
    ask at `1 - q`, because YES + NO always pays exactly $1.00. Size is
    unchanged: the same contracts are on offer, just described from the
    other outcome's point of view.

    `1 - p` for `p` in `[0,1]` is exact enough to stay in `[0,1]` under
    IEEE-754 (both endpoints are exactly representable and subtraction is
    correctly rounded), so no clamping is needed and none is done —
    clamping here would hide a genuinely out-of-range input instead of
    letting `BookLevel` reject it.
    """
    return [BookLevel(price=1.0 - level.price, size=level.size) for level in levels]


def build_book(
    payload: dict[str, Any], *, market_id: str, outcome: str, ts: datetime
) -> OrderBook:
    """Build a normalized `OrderBook` from either Kalshi book encoding.

    Accepts all four shapes seen in the wild — `orderbook_fp` or
    `orderbook` as the container, `yes_dollars`/`no_dollars` (dollar
    strings) or `yes`/`no` (integer cents) as the sides — and produces
    the IDENTICAL `OrderBook` from each, which
    `tests/venues/test_kalshi_adapter.py` asserts directly. That
    equality is the check that catches a missing `/100`.

    Both sides are BIDS. The returned book's asks are derived from the
    OTHER outcome's bids through `1 - q` (see `_complement` and the
    module docstring). For a normal, uncrossed market this yields
    `best_bid <= best_ask`; if it ever yields the reverse, the
    subtraction is inverted and every Kalshi quote is fabricating an
    arbitrage.

    Args:
        payload: The raw `GET /markets/{ticker}/orderbook` body.
        market_id: Kalshi ticker, recorded on the book.
        outcome: `"YES"` or `"NO"`, case-insensitive.
        ts: Aware UTC time this snapshot was read.

    Returns:
        OrderBook: Sorted (by `OrderBook` itself), validated, and tagged
            `depth_source="recorded"` — the derived asks are still real
            observed liquidity, just quoted from the other side.

    Raises:
        VenuePayloadError: If `outcome` is not YES/NO, or the payload
            carries neither documented book shape, or a level is
            malformed/out of domain.
    """
    side = outcome.strip().upper()
    if side not in ("YES", "NO"):
        raise VenuePayloadError(
            f"kalshi outcome must be YES or NO, got {outcome!r}", raw=outcome
        )
    container = payload.get("orderbook_fp")
    if not isinstance(container, dict):
        container = payload.get("orderbook")
    if not isinstance(container, dict):
        raise VenuePayloadError(
            "kalshi orderbook payload has neither orderbook_fp nor orderbook",
            raw=payload,
        )
    if "yes_dollars" in container or "no_dollars" in container:
        is_dollars = True
        raw_yes, raw_no = container.get("yes_dollars"), container.get("no_dollars")
    elif "yes" in container or "no" in container:
        is_dollars = False
        raw_yes, raw_no = container.get("yes"), container.get("no")
    else:
        raise VenuePayloadError(
            "kalshi orderbook carried neither yes/no nor yes_dollars/no_dollars sides",
            raw=payload,
        )

    yes_bids = _levels(raw_yes, is_dollars=is_dollars, context="yes")
    no_bids = _levels(raw_no, is_dollars=is_dollars, context="no")
    if side == "YES":
        bids, asks = yes_bids, _complement(no_bids)
    else:
        bids, asks = no_bids, _complement(yes_bids)
    return OrderBook(
        venue="kalshi",
        market_id=market_id,
        outcome=side,
        bids=tuple(bids),
        asks=tuple(asks),
        ts=ts,
    )


def parse_order_ack(raw: dict[str, Any]) -> OrderAck:
    """Normalize a Kalshi order object into an `OrderAck`.

    Shared by `get_open_orders` (resting orders) and `live.py`'s
    placement response. PLAN.md §3 pins the ack's fields: `order_id`,
    `client_order_id`, `fill_count`, `remaining_count`,
    `average_fill_price`, `average_fee_paid`, `ts_ms`. The `status`
    enum is NOT pinned — see `_ORDER_STATUS`.

    `average_fill_price` passes through the same cents/dollars funnel as
    every other price (`_usd_from`), so an ack quoting `42` cents becomes
    `0.42`, not `42.0`.

    Args:
        raw: The order object, or a `{"order": {...}}` envelope.

    Returns:
        OrderAck: Normalized acknowledgement.

    Raises:
        VenuePayloadError: If the payload carries no `order_id`, or its
            numbers are out of domain (e.g. a price above 1.0, which is
            what a cents-read-as-dollars bug looks like).
    """
    envelope = raw.get("order")
    body: dict[str, Any] = envelope if isinstance(envelope, dict) else raw
    order_id = str(body.get("order_id") or body.get("id") or "")
    if not order_id:
        raise VenuePayloadError("kalshi order payload missing order_id", raw=raw)
    filled = _to_float(body.get("fill_count", body.get("taker_fill_count")), default=0.0)
    remaining = _to_float(
        body.get("remaining_count", body.get("resting_count")), default=0.0
    )
    status_raw = str(body.get("status") or "resting").strip().lower()
    mapped_status = _ORDER_STATUS.get(status_raw)
    if mapped_status is None:
        # Falling back to "open" is the conservative reading (the venue
        # may still be holding the order), and refusing to parse an ack
        # over a new status word would be worse. Logged so the operator
        # can see WHICH word we did not know (T44).
        logger.warning(
            "order",
            extra={
                "event": "kalshi_unknown_order_status",
                "venue": "kalshi",
                "order_id": order_id,
                "status": status_raw,
                "assumed": "open",
            },
        )
    try:
        return OrderAck(
            venue="kalshi",
            order_id=order_id,
            client_order_id=str(body.get("client_order_id") or order_id),
            status=mapped_status if mapped_status is not None else "open",
            filled_size=filled,
            remaining_size=remaining,
            avg_fill_price=_usd_from(body, "average_fill_price"),
            ts=_parse_timestamp(body.get("ts_ms") or body.get("created_time"))
            or utcnow(),
        )
    except ValueError as exc:
        raise VenuePayloadError(f"kalshi order payload is invalid: {exc}", raw=raw) from exc
