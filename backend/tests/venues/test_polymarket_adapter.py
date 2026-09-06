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
import logging
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


# ---------------------------------------------------------------------------
# T44. Divergence: what happens when a real payload disagrees with the pin.
#
# Every case below was CHARACTERIZED against the shipped adapter first
# (through `httpx.MockTransport` and a stubbed CLOB client -- never a
# venue, GUARDRAILS.md §1.4) and then split in two: the ones that
# produced a plausible-looking WRONG number in silence, which now raise,
# and the ones that were already correct or already loud, which are
# pinned so a later "hardening" pass cannot turn tolerance into
# brittleness.
# ---------------------------------------------------------------------------


class _FakeClobClient:
    """The three `py_clob_client` reads this adapter makes, canned.

    Lets the credentialed methods be exercised with divergent payloads
    without a network call, a real client, or a real credential
    (GUARDRAILS.md §1.3/§1.4).
    """

    def __init__(
        self,
        *,
        balance: Any = None,
        orders: Any = None,
        trades: Any = None,
    ) -> None:
        self._balance = {} if balance is None else balance
        self._orders = [] if orders is None else orders
        self._trades = [] if trades is None else trades

    def get_balance_allowance(self, params: Any) -> Any:  # noqa: ARG002
        return self._balance

    def get_orders(self, params: Any) -> Any:  # noqa: ARG002
        return self._orders

    def get_trades(self, params: Any) -> Any:  # noqa: ARG002
        return self._trades


class _FakeWrapper:
    """Stands in for `ClobClientWrapper`, exposing only `.client`."""

    def __init__(self, client: _FakeClobClient) -> None:
        self.client = client


def credentialed_adapter(
    monkeypatch: pytest.MonkeyPatch,
    *,
    balance: Any = None,
    orders: Any = None,
    trades: Any = None,
) -> PolymarketAdapter:
    """Build an adapter whose credentialed reads answer from `_FakeClobClient`.

    The private key is an all-zeros placeholder (GUARDRAILS.md §1.3:
    fixtures use `0x` + zeros, never a real key) and exists only so
    `_ensure_clob_wrapper`'s credential gate passes; the wrapper it would
    otherwise build is replaced outright, so `py_clob_client` is never
    constructed and nothing is signed or sent. The HTTP transport is the
    refusing one -- if any of these paths ever reaches the network, the
    test fails rather than escaping.
    """
    from pydantic import SecretStr

    from app.config import settings

    monkeypatch.setattr(
        settings, "polymarket_private_key", SecretStr("0x" + "0" * 64)
    )
    adapter = PolymarketAdapter(transport=_refusing_transport())
    monkeypatch.setattr(
        adapter,
        "_clob_wrapper",
        _FakeWrapper(_FakeClobClient(balance=balance, orders=orders, trades=trades)),
    )
    return adapter


def _gamma_with(**overrides: Any) -> list[dict[str, Any]]:
    """The Gamma fixture list with market a001's payload overridden.

    A key whose value is `_ABSENT` is DELETED rather than overridden, so
    "the venue stopped sending this field" can be expressed.
    """
    market = dict(GAMMA_MARKETS[0])
    for key, value in overrides.items():
        if value is _ABSENT:
            market.pop(key, None)
        else:
            market[key] = value
    return [market, *GAMMA_MARKETS[1:]]


#: Sentinel for `_gamma_with`: delete this key instead of setting it.
_ABSENT = object()


# -- Market metadata --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_market_with_no_outcomes_is_refused() -> None:
    """A renamed/absent `outcomes` must not list a market with none.

    It used to default to `[]`, producing a `VenueMarket` with
    `outcomes=()` and `outcome_ids={}` -- a market the scanner would
    happily rank and the matcher would happily read, that cannot be
    quoted or traded, with nothing raised. The shared contract suite
    already asserts every listed market HAS outcomes; this stops the
    adapter manufacturing one that does not.
    """
    adapter = _adapter(gamma_markets=_gamma_with(outcomes=_ABSENT))

    with pytest.raises(VenuePayloadError) as excinfo:
        await adapter.get_market(MARKET_A001)

    assert "outcomes" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_string_boolean_does_not_mark_a_live_market_resolved() -> None:
    """`"resolved": "false"` is FALSE, not `bool("false") is True`.

    This is the whole "a string where a bool was assumed" class, and its
    old behaviour was maximally quiet: every live market would have come
    back `status="resolved"`, been filtered out of every `status="open"`
    scan, and produced an empty opportunity list that looks exactly like
    a quiet market.
    """
    adapter = _adapter(gamma_markets=_gamma_with(resolved="false", closed="false"))

    market = await adapter.get_market(MARKET_A001)

    assert market.status == "open"


@pytest.mark.asyncio
async def test_a_boolean_flag_that_is_not_a_boolean_is_refused() -> None:
    """A flag we cannot read is an error, not a guess."""
    adapter = _adapter(gamma_markets=_gamma_with(resolved="maybe"))

    with pytest.raises(VenuePayloadError) as excinfo:
        await adapter.get_market(MARKET_A001)

    assert "resolved" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_null_question_does_not_become_the_string_none() -> None:
    """`str(payload.get("question", ""))` on a null returned "None".

    A market titled `"None"` is displayed, matched and scored as if that
    were its question -- untrusted venue text (GUARDRAILS.md §6) that the
    venue never sent.
    """
    adapter = _adapter(gamma_markets=_gamma_with(question=None))

    market = await adapter.get_market(MARKET_A001)

    assert market.question == ""


@pytest.mark.asyncio
async def test_a_market_the_domain_type_rejects_is_a_venue_error() -> None:
    """A CLOB `tick_size: 0` raises `VenuePayloadError`, not a bare `ValueError`.

    `VenueMarket`'s validator names the field but raises a plain
    `ValueError`, which is not a `VenueError` and so is outside
    `app.services.scanner.VENUE_READ_FAULTS`: one such market would abort
    a whole scan pass instead of being skipped.
    """
    adapter = _adapter(clob_market=dict(CLOB_MARKET, tick_size=0))

    with pytest.raises(VenuePayloadError) as excinfo:
        await adapter.get_market(MARKET_A001)

    assert "tick_size" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_fee_rate_where_basis_points_were_expected_is_refused() -> None:
    """`taker_base_fee: 0.02` is a RATE; /10_000 would make it 0.000002.

    A fee of two parts per million is not a plausible fee, it is a unit
    change -- and it is the input that makes every marginal edge look
    profitable. Polymarket's analogue of the Kalshi cents/dollars slip.
    """
    adapter = _adapter(clob_market=dict(CLOB_MARKET, taker_base_fee=0.02))

    with pytest.raises(VenuePayloadError) as excinfo:
        await adapter.get_market(MARKET_A001)

    assert "taker_base_fee" in str(excinfo.value)
    assert "BASIS POINTS" in str(excinfo.value)


@pytest.mark.asyncio
async def test_an_explicit_zero_fee_is_still_a_waiver_not_an_error() -> None:
    """TOLERANCE PIN. `taker_base_fee: 0` is a real, declared fee waiver.

    The unit guard fires on `(0, 1)` EXCLUSIVE precisely so that a whole
    zero -- which the adapter has always treated as an authoritative
    waiver, `is not None` rather than truthiness -- keeps working.
    """
    adapter = _adapter(clob_market=dict(CLOB_MARKET, taker_base_fee=0))

    market = await adapter.get_market(MARKET_A001)

    assert market.fee.source == "clob_market"
    assert market.fee.taker_rate == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_a_string_encoded_basis_point_fee_is_still_accepted() -> None:
    """TOLERANCE PIN. `"200"` is 200 bps -- a type change, not a unit change."""
    adapter = _adapter(clob_market=dict(CLOB_MARKET, taker_base_fee="200"))

    market = await adapter.get_market(MARKET_A001)

    assert market.fee.taker_rate == pytest.approx(0.02)  # 200 bps / 10_000


@pytest.mark.asyncio
async def test_extra_unknown_keys_are_tolerated_everywhere() -> None:
    """TOLERANCE PIN. Venues ADD fields; that must never be an outage."""
    adapter = _adapter(
        gamma_markets=_gamma_with(brand_new_field={"nested": [1, 2]}),
        clob_book=dict(CLOB_BOOK, unknown_top_level="hello"),
    )

    market = await adapter.get_market(MARKET_A001)
    book = await adapter.get_book(MARKET_A001, "Yes")

    assert market.market_id == MARKET_A001
    assert book.bids[0].price == pytest.approx(0.40)


# -- The book ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_book_timestamp_in_seconds_is_not_dated_to_1970() -> None:
    """`1767225600` (seconds) must not be divided by 1000 a second time.

    PLAN.md §3 pins milliseconds, and the old parser hard-divided by
    1000: a seconds-valued epoch produced 1970-01-21, a 56-year-old
    "snapshot" with nothing raised. Both `_try_epoch` here and Kalshi's
    `_parse_timestamp` already distinguish the two by magnitude; the book
    parser now does too, so the divergence produces the CORRECT time
    rather than an error.
    """
    adapter = _adapter(clob_book=dict(CLOB_BOOK, timestamp=1767225600))

    book = await adapter.get_book(MARKET_A001, "Yes")

    assert book.ts == datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_a_book_with_no_parseable_timestamp_is_refused() -> None:
    """A book with no read time cannot be aged, and `now()` would lie."""
    adapter = _adapter(clob_book={k: v for k, v in CLOB_BOOK.items() if k != "timestamp"})

    with pytest.raises(VenuePayloadError) as excinfo:
        await adapter.get_book(MARKET_A001, "Yes")

    assert "timestamp" in str(excinfo.value)


@pytest.mark.asyncio
async def test_an_empty_book_side_is_tolerated() -> None:
    """TOLERANCE PIN. A one-sided book is a real, common venue state."""
    adapter = _adapter(clob_book=dict(CLOB_BOOK, bids=[]))

    book = await adapter.get_book(MARKET_A001, "Yes")

    assert book.bids == ()
    assert book.asks[0].price == pytest.approx(0.42)


@pytest.mark.asyncio
async def test_a_market_without_token_ids_names_the_field_it_is_missing() -> None:
    """The error must point at `clobTokenIds`, not at the caller's outcome.

    Without token ids the book endpoint (keyed by `token_id`) cannot be
    addressed at all, and the old message -- "unknown outcome 'Yes'" --
    sent the reader looking for a typo in their own call.
    """
    adapter = _adapter(gamma_markets=_gamma_with(clobTokenIds=_ABSENT))

    with pytest.raises(VenuePayloadError) as excinfo:
        await adapter.get_book(MARKET_A001, "Yes")

    assert "clobTokenIds" in str(excinfo.value)


# -- Credentialed reads -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_balance_payload_without_an_amount_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing `balance` used to report a funded account as $0.00.

    `_to_float(..., default=0.0)` produced a number no caller can tell
    from a real zero balance, on the one field that decides how much
    capital exists. Kalshi's `get_balance` has always raised here.
    """
    adapter = credentialed_adapter(monkeypatch, balance={"asset_type": "COLLATERAL"})

    with pytest.raises(VenuePayloadError) as excinfo:
        await adapter.get_balance()

    assert "balance" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_string_balance_is_still_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """TOLERANCE PIN. The CLOB sends numbers as decimal strings."""
    adapter = credentialed_adapter(monkeypatch, balance={"balance": "1234.56"})

    balance = await adapter.get_balance()

    assert balance.available == pytest.approx(1234.56)


@pytest.mark.asyncio
async def test_a_cents_priced_trade_history_does_not_become_a_dollar_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trade priced `42` must not net to a position at `avg_price=1.0`.

    The old derivation CLAMPED (`min(max(x, 0.0), 1.0)`), so an
    out-of-domain price -- a cents-encoded payload, the exact failure
    NOTES.md records as a -$17,150 fee -- produced a real-looking cost
    basis of $1.00 per contract on a position used for sizing, silently.
    """
    adapter = credentialed_adapter(
        monkeypatch,
        trades=[
            {"market": "m1", "asset_id": "t1", "side": "BUY", "price": 42, "size": 10}
        ],
    )

    with pytest.raises(VenuePayloadError) as excinfo:
        await adapter.get_positions()

    assert "polymarket" in str(excinfo.value)
    assert "42.0" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_trade_history_that_never_parses_is_not_reported_as_flat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two trades in, zero positions out, no error -- that was the answer."""
    adapter = credentialed_adapter(
        monkeypatch,
        trades=[{"market": "m1"}, {"asset_id": "t1"}],
    )

    with pytest.raises(VenuePayloadError) as excinfo:
        await adapter.get_positions()

    assert "NONE parsed" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_closed_out_position_is_still_reported_as_no_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TOLERANCE PIN. Trades that NET to zero are not a parse failure.

    The drop-everything guard must fire on a schema disagreement, not on
    an account that simply closed what it opened.
    """
    adapter = credentialed_adapter(
        monkeypatch,
        trades=[
            {"market": "m1", "asset_id": "t1", "side": "BUY", "price": 0.4, "size": 10},
            {"market": "m1", "asset_id": "t1", "side": "SELL", "price": 0.5, "size": 10},
        ],
    )

    assert await adapter.get_positions() == []


@pytest.mark.asyncio
async def test_a_trade_with_an_unreadable_side_is_not_netted_as_a_sell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`side` was `"BUY"` or ELSE-A-SELL, so an unknown word erased a buy."""
    adapter = credentialed_adapter(
        monkeypatch,
        trades=[
            {
                "market": "m1",
                "asset_id": "t1",
                "side": "buy_yes",
                "price": 0.4,
                "size": 10,
            }
        ],
    )

    with pytest.raises(VenuePayloadError) as excinfo:
        await adapter.get_positions()

    assert "NONE parsed" in str(excinfo.value)


@pytest.mark.asyncio
async def test_an_order_entry_without_an_id_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ack with an empty `order_id` is indexed under "" by reconcile.

    Kalshi's `parse_order_ack` has always refused this; Polymarket's
    built the ack anyway, with `order_id=""` and
    `client_order_id="unknown"`.
    """
    adapter = credentialed_adapter(monkeypatch, orders=[{"status": "open"}])

    with pytest.raises(VenuePayloadError):
        await adapter.get_open_orders()


@pytest.mark.asyncio
async def test_a_present_but_unreadable_size_is_not_read_as_unfilled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`size_matched: "abc"` used to become `0.0` -- a filled order, untouched.

    `_try_float(...) or 0.0` could not tell "the venue did not send this"
    from "the venue sent something I cannot read", so a partly-filled
    order came back as `filled_size=0.0`: a number reconciliation acts
    on. The entry is now refused, naming the field, which `get_open_orders`
    logs and skips (a second, well-formed order still parses -- one bad
    row is not an outage).
    """
    adapter = credentialed_adapter(
        monkeypatch,
        orders=[
            {"id": "o1", "original_size": "10", "size_matched": "abc"},
            {"id": "o2", "original_size": "10", "size_matched": "4"},
        ],
    )

    with caplog.at_level(logging.WARNING, logger="app.venues.polymarket.adapter"):
        acks = await adapter.get_open_orders()

    assert [a.order_id for a in acks] == ["o2"]
    assert acks[0].filled_size == pytest.approx(4.0)
    assert any(
        "size_matched" in str(record.__dict__.get("reason", ""))
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_an_absent_optional_size_still_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TOLERANCE PIN. ABSENT keeps its fallback; only PRESENT-and-unreadable raises.

    `py_clob_client.get_orders` has no pinned schema, so an order that
    simply does not carry `size_matched` must still parse.
    """
    adapter = credentialed_adapter(
        monkeypatch, orders=[{"id": "o1", "original_size": "10", "price": "0.4"}]
    )

    acks = await adapter.get_open_orders()

    assert acks[0].filled_size == pytest.approx(0.0)
    assert acks[0].remaining_size == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_an_unknown_order_status_stays_open_and_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """TOLERANCE PIN + a signal, matching Kalshi's handling of the same case."""
    adapter = credentialed_adapter(
        monkeypatch, orders=[{"id": "o1", "status": "expired", "original_size": "10"}]
    )

    with caplog.at_level(logging.WARNING, logger="app.venues.polymarket.adapter"):
        acks = await adapter.get_open_orders()

    assert acks[0].status == "open"
    assert any(
        record.__dict__.get("event") == "polymarket_unknown_order_status"
        and record.__dict__.get("status") == "expired"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_a_fill_with_no_fee_rate_is_estimated_not_zeroed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `fee_rate_bps` used to mean `$0.00` of fee, silently.

    Kalshi's fills already refuse to do this -- "a fabricated zero fee is
    exactly the input that makes a marginal edge look profitable" -- and
    label the estimate in `metadata["fee_source"]`. Polymarket now
    matches, using the conservative category-table rate.

    By hand, at the 0.05 fallback rate:
    10 x 0.05 x 0.40 x (1 - 0.40) = 0.12
    """
    adapter = credentialed_adapter(
        monkeypatch,
        trades=[
            {
                "id": "t1",
                "market": "m1",
                "asset_id": "a1",
                "price": "0.40",
                "size": "10",
                "match_time": 1767225600,
            }
        ],
    )

    fills = await adapter.get_fills(datetime(2026, 1, 1, tzinfo=UTC))

    assert fills[0].fee == pytest.approx(0.12)
    assert fills[0].metadata["fee_source"] == "category_estimate"


@pytest.mark.asyncio
async def test_a_fill_with_a_venue_fee_rate_is_labelled_as_such(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TOLERANCE PIN. A trade carrying its own bps still uses them.

    By hand, 200 bps = 0.02: 10 x 0.02 x 0.40 x (1 - 0.40) = 0.048
    """
    adapter = credentialed_adapter(
        monkeypatch,
        trades=[
            {
                "id": "t1",
                "market": "m1",
                "asset_id": "a1",
                "price": "0.40",
                "size": "10",
                "fee_rate_bps": 200,
                "match_time": 1767225600,
            }
        ],
    )

    fills = await adapter.get_fills(datetime(2026, 1, 1, tzinfo=UTC))

    assert fills[0].fee == pytest.approx(0.048)
    assert fills[0].metadata["fee_source"] == "venue_rate"


@pytest.mark.asyncio
async def test_a_fill_with_no_timestamp_is_dropped_not_stamped_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invented `utcnow()` makes an OLD fill pass a `since` filter.

    Kalshi drops these ("it cannot be placed on either side of `since`").
    Here the whole payload is unparseable, so the drop-everything guard
    turns it into an error rather than an empty fill list.
    """
    adapter = credentialed_adapter(
        monkeypatch,
        trades=[{"id": "t1", "market": "m1", "price": "0.40", "size": "10"}],
    )

    with pytest.raises(VenuePayloadError) as excinfo:
        await adapter.get_fills(datetime(2026, 1, 1, tzinfo=UTC))

    assert "NONE parsed" in str(excinfo.value)


@pytest.mark.asyncio
async def test_one_unparseable_trade_among_good_ones_is_skipped_not_fatal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """TOLERANCE PIN. One bad row must not cost the whole read -- but it is logged."""
    adapter = credentialed_adapter(
        monkeypatch,
        trades=[
            {"id": "bad"},
            {
                "id": "t1",
                "market": "m1",
                "asset_id": "a1",
                "price": "0.40",
                "size": "10",
                "fee_rate_bps": 200,
                "match_time": 1767225600,
            },
        ],
    )

    with caplog.at_level(logging.WARNING, logger="app.venues.polymarket.adapter"):
        fills = await adapter.get_fills(datetime(2026, 1, 1, tzinfo=UTC))

    assert [f.order_id for f in fills] == ["t1"]
    assert any(
        record.__dict__.get("event") == "polymarket_fill_entry_skipped"
        for record in caplog.records
    )


# -- Builders the shared contract suite reuses (T44) ------------------------


def adapter_with_a_market_the_domain_type_rejects() -> PolymarketAdapter:
    """A Polymarket adapter whose CLOB enrichment carries `tick_size: 0`."""
    return _adapter(clob_market=dict(CLOB_MARKET, tick_size=0))


def adapter_with_a_balance_payload_missing_its_amount(
    monkeypatch: pytest.MonkeyPatch,
) -> PolymarketAdapter:
    """A Polymarket adapter whose balance payload carries no amount field."""
    return credentialed_adapter(monkeypatch, balance={"asset_type": "COLLATERAL"})


def adapter_with_orders_that_never_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> PolymarketAdapter:
    """A Polymarket adapter whose open orders all lack an id."""
    return credentialed_adapter(
        monkeypatch, orders=[{"status": "open"}, {"status": "open"}]
    )


def adapter_with_extra_unknown_keys() -> PolymarketAdapter:
    """A Polymarket adapter whose market and book payloads carry unknown fields."""
    return _adapter(
        gamma_markets=_gamma_with(brand_new_field={"nested": [1, 2]}),
        clob_book=dict(CLOB_BOOK, unknown_top_level="hello"),
    )
