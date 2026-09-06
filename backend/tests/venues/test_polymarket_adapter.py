"""Tests for `app.venues.polymarket` (T11).

Derived from TASKS.md T11's acceptance lines and brief, not from the
implementation:
  1. Markets parse: Gamma `/markets` (+ CLOB market enrichment) maps into
     `VenueMarket` — question/outcomes/outcome_ids/rules_text/
     resolution_source/close_time/status all populated, `tick_size`/
     `min_size` sourced from the CLOB market payload.
  2. Book parses: CLOB `/book` decimal-string prices/sizes become
     `float`, `timestamp` (ms) becomes aware UTC, `bids`/`asks` sort per
     `OrderBook`'s own contract.
  3. Fee-schedule source precedence: a market whose CLOB payload carries
     `taker_base_fee` gets `source="clob_market"` (overriding the
     category default); one that doesn't falls back to
     `source="category_table"`.
  4. `VenuePayloadError` on a book missing `asks`.
  5. Constructing `PolymarketLiveAdapter` under default settings raises
     `LiveTradingDisabled` (GUARDRAILS.md §1.1/§1.2) — and, per the
     GUARDRAILS.md §1.2 test pattern, an explicit permissive `Settings`
     object (never the environment) is what proves the fence CAN open.

All network access here is `httpx.MockTransport` against hand-written
fixtures under `tests/fixtures/polymarket/` (GUARDRAILS.md §1.4: no
network to venues, ever, from a test).
"""
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.execution.fences import LiveTradingDisabled
from app.venues.base import VenueAuthError, VenuePayloadError
from app.venues.polymarket.adapter import PolymarketAdapter
from app.venues.polymarket.live import PolymarketLiveAdapter

FIXTURES = Path(__file__).parent.parent / "fixtures" / "polymarket"


def _load(name: str) -> Any:
    with open(FIXTURES / name) as f:
        return json.load(f)


GAMMA_MARKETS: list[dict[str, Any]] = _load("gamma_markets.json")
CLOB_BOOK: dict[str, Any] = _load("clob_book.json")
CLOB_MARKET: dict[str, Any] = _load("clob_market.json")

MARKET_A001 = "0x0000000000000000000000000000000000000000000000000000000000a001"
MARKET_A002 = "0x0000000000000000000000000000000000000000000000000000000000a002"
MARKET_A003 = "0x0000000000000000000000000000000000000000000000000000000000a003"


def _make_transport(
    *,
    gamma_markets: list[dict[str, Any]] | None = None,
    clob_book: dict[str, Any] | None = None,
    clob_market: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> httpx.MockTransport:
    """Build a `MockTransport` routing Gamma/CLOB requests to fixture data.

    Args:
        gamma_markets: Response for Gamma `GET /markets` (optionally
            filtered by a `condition_ids` query param, mimicking a
            single-market lookup). Defaults to the full fixture list.
        clob_book: Response for CLOB `GET /book`. Defaults to the fixture.
        clob_market: Response for CLOB `GET /markets/{condition_id}`
            (single) and the `data` entry of `GET /markets` (list, used
            by `list_markets`'s bulk enrichment). Defaults to the
            fixture; pass `None` explicitly via an empty dict `{}` is
            not the same as omitting it — see call sites.
        events: Response for Gamma `GET /events`. Defaults to `[]` (no
            multi-outcome event groupings).
    """
    gamma_markets = GAMMA_MARKETS if gamma_markets is None else gamma_markets
    clob_book = CLOB_BOOK if clob_book is None else clob_book
    clob_market = CLOB_MARKET if clob_market is None else clob_market
    events = [] if events is None else events

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "gamma-api.polymarket.com":
            if url.path == "/markets":
                condition_ids = url.params.get("condition_ids")
                if condition_ids:
                    matches = [
                        m for m in gamma_markets if m.get("conditionId") == condition_ids
                    ]
                    return httpx.Response(200, json=matches)
                return httpx.Response(200, json=gamma_markets)
            if url.path == "/events":
                return httpx.Response(200, json=events)
        elif url.host == "clob.polymarket.com":
            if url.path == "/book":
                return httpx.Response(200, json=clob_book)
            if url.path == "/markets":
                return httpx.Response(
                    200, json={"data": [clob_market], "next_cursor": "LTE="}
                )
            if url.path.startswith("/markets/"):
                condition_id = url.path.removeprefix("/markets/")
                if condition_id == clob_market.get("condition_id"):
                    return httpx.Response(200, json=clob_market)
                return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(404, json={"error": "unhandled", "url": str(url)})

    return httpx.MockTransport(handler)


def _adapter(**kwargs: Any) -> PolymarketAdapter:
    return PolymarketAdapter(transport=_make_transport(**kwargs))


# ---------------------------------------------------------------------------
# 1. Markets parse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_markets_parses_gamma_payload() -> None:
    """All three fixture markets parse with correct field mapping."""
    adapter = _adapter()

    markets = await adapter.list_markets()

    assert len(markets) == 3
    by_id = {m.market_id: m for m in markets}

    m1 = by_id[MARKET_A001]
    assert m1.question == "Will Team Alpha win the championship?"
    assert m1.outcomes == ("Yes", "No")
    assert m1.outcome_ids == {
        "Yes": "1000000000000000000000000000000000000000000000000000000000000001",
        "No": "1000000000000000000000000000000000000000000000000000000000000002",
    }
    assert m1.rules_text.startswith("This market resolves YES if Team Alpha")
    assert m1.resolution_source == "https://example-league.test/results"
    assert m1.close_time == datetime(2026, 12, 31, 23, 59, tzinfo=UTC)
    assert m1.status == "open"
    assert m1.result is None
    # CLOB market enrichment (bulk /markets, keyed by condition_id):
    assert m1.tick_size == pytest.approx(0.01)
    assert m1.min_size == pytest.approx(5.0)

    m2 = by_id[MARKET_A002]
    assert m2.status == "closed"
    assert m2.result is None

    m3 = by_id[MARKET_A003]
    assert m3.status == "resolved"
    assert m3.result == "Yes"  # outcomePrices ["1", "0"] -> "Yes" wins


@pytest.mark.asyncio
async def test_list_markets_status_filter() -> None:
    """`status=` filters the returned markets by their parsed status."""
    adapter = _adapter()

    open_markets = await adapter.list_markets(status="open")

    assert {m.market_id for m in open_markets} == {MARKET_A001}


@pytest.mark.asyncio
async def test_get_market_single_lookup() -> None:
    """`get_market` resolves one market by condition id via Gamma's filter."""
    adapter = _adapter()

    market = await adapter.get_market(MARKET_A001)

    assert market.market_id == MARKET_A001
    assert market.question == "Will Team Alpha win the championship?"


@pytest.mark.asyncio
async def test_get_market_unknown_id_raises_venue_payload_error() -> None:
    """`get_market` on an id Gamma has no record of raises `VenuePayloadError`."""
    adapter = _adapter()

    with pytest.raises(VenuePayloadError):
        await adapter.get_market("0x" + "9" * 64)


# ---------------------------------------------------------------------------
# 2. Book parses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_book_parses_clob_payload() -> None:
    """CLOB `/book` decimal strings -> float; ms timestamp -> aware UTC."""
    adapter = _adapter()

    book = await adapter.get_book(MARKET_A001, "Yes")

    assert book.venue == "polymarket"
    assert book.market_id == MARKET_A001
    assert book.outcome == "Yes"
    # best bid first (descending), best ask first (ascending) -- OrderBook
    # normalizes on construction regardless of input order.
    assert book.bids[0].price == pytest.approx(0.40)
    assert book.bids[0].size == pytest.approx(120.5)
    assert book.bids[1].price == pytest.approx(0.39)
    assert book.asks[0].price == pytest.approx(0.42)
    assert book.asks[0].size == pytest.approx(80.0)
    assert book.asks[1].price == pytest.approx(0.43)
    # "1767225600000" ms -> 2026-01-01T00:00:00Z
    assert book.ts == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert book.depth_source == "recorded"
    # book-payload tick_size/min_order_size go on OrderBook.metadata, NOT
    # on OrderBook itself (orchestrator ruling: those fields live on
    # VenueMarket, populated separately).
    assert book.metadata["tick_size"] == "0.01"
    assert book.metadata["min_order_size"] == "5"
    assert book.metadata["last_trade_price"] == "0.42"
    assert not hasattr(book, "tick_size")
    assert not hasattr(book, "min_size")


@pytest.mark.asyncio
async def test_get_book_unknown_outcome_raises_venue_payload_error() -> None:
    """An outcome name not in the market's `outcome_ids` is a `VenuePayloadError`."""
    adapter = _adapter()

    with pytest.raises(VenuePayloadError):
        await adapter.get_book(MARKET_A001, "Maybe")


# ---------------------------------------------------------------------------
# 3. Fee-schedule source precedence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fee_schedule_prefers_clob_market_when_present() -> None:
    """Market a001's CLOB payload carries `taker_base_fee=200` (bps) ->
    `source="clob_market"`, rate 0.02 -- overriding Sports' 0.05 category
    default.
    """
    adapter = _adapter()

    market = await adapter.get_market(MARKET_A001)

    assert market.fee.source == "clob_market"
    assert market.fee.taker_rate == pytest.approx(0.02)  # 200 bps / 10_000
    assert market.fee.maker_rate == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_fee_schedule_falls_back_to_category_table_without_clob_fee() -> None:
    """Market a002 has no CLOB market entry in this fixture set (only a001
    does) -> falls back to the Politics category rate, 0.04.
    """
    adapter = _adapter()

    market = await adapter.get_market(MARKET_A002)

    assert market.fee.source == "category_table"
    assert market.fee.taker_rate == pytest.approx(0.04)  # Politics


@pytest.mark.asyncio
async def test_fee_schedule_unknown_category_uses_conservative_fallback() -> None:
    """Market a003's category "Weird" is not in the category table ->
    the conservative 0.05 fallback, still `source="category_table"`.
    """
    adapter = _adapter()

    market = await adapter.get_market(MARKET_A003)

    assert market.fee.source == "category_table"
    assert market.fee.taker_rate == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_list_markets_fee_precedence_across_all_three() -> None:
    """One `list_markets` call exercises both branches at once: a001 (bulk
    CLOB enrichment present) gets `"clob_market"`; a002/a003 (no bulk CLOB
    entry) fall back to `"category_table"`.
    """
    adapter = _adapter()

    markets = await adapter.list_markets()
    by_id = {m.market_id: m for m in markets}

    assert by_id[MARKET_A001].fee.source == "clob_market"
    assert by_id[MARKET_A002].fee.source == "category_table"
    assert by_id[MARKET_A003].fee.source == "category_table"


# ---------------------------------------------------------------------------
# 4. VenuePayloadError on a book missing `asks`
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_book_missing_asks_raises_venue_payload_error() -> None:
    """A CLOB book payload with `bids` but no `asks` key is malformed."""
    malformed_book = {k: v for k, v in CLOB_BOOK.items() if k != "asks"}
    adapter = _adapter(clob_book=malformed_book)

    with pytest.raises(VenuePayloadError):
        await adapter.get_book(MARKET_A001, "Yes")


@pytest.mark.asyncio
async def test_get_book_missing_bids_raises_venue_payload_error() -> None:
    """Symmetric case: `asks` present, `bids` missing."""
    malformed_book = {k: v for k, v in CLOB_BOOK.items() if k != "bids"}
    adapter = _adapter(clob_book=malformed_book)

    with pytest.raises(VenuePayloadError):
        await adapter.get_book(MARKET_A001, "Yes")


@pytest.mark.asyncio
async def test_get_book_non_json_body_raises_venue_payload_error() -> None:
    """A 200 whose body is not JSON at all is the VENUE's fault (T38 F2).

    A CDN error page or a truncated response makes `response.json()`
    raise `json.JSONDecodeError` -- a `ValueError`. Left unflattened,
    no caller could skip it without also catching every genuine
    `ValueError` a programming error would raise, so
    `app.services.scanner`'s per-book skip class could not include it
    and ONE such body would abort a whole 800-book scan pass. Flattened
    at the adapter boundary it is an ordinary `VenueError`, exactly as
    Kalshi's `json_object` helper has always done for the same case.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "gamma-api.polymarket.com":
            if url.path == "/markets":
                condition_ids = url.params.get("condition_ids")
                if condition_ids:
                    return httpx.Response(
                        200,
                        json=[
                            m
                            for m in GAMMA_MARKETS
                            if m.get("conditionId") == condition_ids
                        ],
                    )
                return httpx.Response(200, json=GAMMA_MARKETS)
            if url.path == "/events":
                return httpx.Response(200, json=[])
        if url.host == "clob.polymarket.com":
            if url.path == "/book":
                # A 200 carrying an HTML error page, as a CDN or a
                # misrouted request produces.
                return httpx.Response(
                    200,
                    text="<html><body>502 Bad Gateway</body></html>",
                    headers={"content-type": "text/html"},
                )
            if url.path.startswith("/markets"):
                return httpx.Response(200, json=CLOB_MARKET)
        return httpx.Response(404, json={"error": "unhandled"})

    adapter = PolymarketAdapter(transport=httpx.MockTransport(handler))

    with pytest.raises(VenuePayloadError):
        await adapter.get_book(MARKET_A001, "Yes")
    await adapter.aclose()


# ---------------------------------------------------------------------------
# 5. Live-trading fence
# ---------------------------------------------------------------------------


def _refusing_transport() -> httpx.MockTransport:
    """A transport that fails any test relying on it for real network I/O.

    Used for fence-only tests (constructor behavior), which must never
    reach the network regardless of how the fence resolves.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network call in a fence-only test: {request.url}")

    return httpx.MockTransport(handler)


def test_polymarket_live_adapter_disabled_by_default_settings() -> None:
    """Constructing `PolymarketLiveAdapter` under the process's default,
    paper-mode `Settings` (GUARDRAILS.md §1.2: always paper in tests)
    raises `LiveTradingDisabled` before anything else happens.
    """
    with pytest.raises(LiveTradingDisabled):
        PolymarketLiveAdapter(transport=_refusing_transport())


def test_polymarket_live_adapter_disabled_with_mode_but_no_confirmation() -> None:
    """`trading_mode="live"` ALONE, with no confirmation string, still trips
    the fence -- both conditions are required together.
    """
    permissive_mode_only = Settings(TRADING_MODE="live", LIVE_TRADING_CONFIRMATION="")

    with pytest.raises(LiveTradingDisabled):
        PolymarketLiveAdapter(
            transport=_refusing_transport(), settings_obj=permissive_mode_only
        )


def test_polymarket_live_adapter_disabled_with_confirmation_but_paper_mode() -> None:
    """The confirmation phrase ALONE, with `trading_mode="paper"`, also
    still trips the fence.
    """
    permissive_confirmation_only = Settings(
        TRADING_MODE="paper", LIVE_TRADING_CONFIRMATION="I_UNDERSTAND_REAL_MONEY"
    )

    with pytest.raises(LiveTradingDisabled):
        PolymarketLiveAdapter(
            transport=_refusing_transport(), settings_obj=permissive_confirmation_only
        )


def test_polymarket_live_adapter_constructs_with_explicit_permissive_settings() -> None:
    """The fence CAN open: an explicit `Settings(...)` object (GUARDRAILS.md
    §1.2 pattern -- never the environment) with BOTH conditions satisfied
    lets construction succeed. This does not itself place any order.
    """
    permissive = Settings(
        TRADING_MODE="live", LIVE_TRADING_CONFIRMATION="I_UNDERSTAND_REAL_MONEY"
    )

    adapter = PolymarketLiveAdapter(
        transport=_refusing_transport(), settings_obj=permissive
    )

    assert adapter.venue == "polymarket"


# ---------------------------------------------------------------------------
# Credentialed methods: VenueAuthError (not ValueError) without credentials
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name",
    ["get_balance", "get_positions", "get_open_orders"],
)
async def test_credentialed_methods_raise_venue_auth_error_without_key(
    method_name: str,
) -> None:
    """With `settings.polymarket_private_key` empty (the default in every
    test process -- GUARDRAILS.md §1.2/§1.3), every credentialed method
    raises `VenueAuthError`, not `ValueError`, and never touches the
    network to do so.
    """
    adapter = PolymarketAdapter(transport=_refusing_transport())
    method = getattr(adapter, method_name)

    with pytest.raises(VenueAuthError):
        await method()


@pytest.mark.asyncio
async def test_get_fills_raises_venue_auth_error_without_key() -> None:
    """`get_fills` takes a `since` argument; same credential gate applies."""
    adapter = PolymarketAdapter(transport=_refusing_transport())

    with pytest.raises(VenueAuthError):
        await adapter.get_fills(datetime(2026, 1, 1, tzinfo=UTC))
