"""Tests for `app.venues.kalshi` (T12).

Derived from TASKS.md T12's acceptance lines and brief, not from the
implementation:

  1. AUTH. `sign_request` produces a signature the matching PUBLIC key
     verifies, over `f"{ts_ms}{METHOD}{path}"` with the QUERY STRING
     STRIPPED and the `/trade-api/v2` prefix INCLUDED, and the timestamp
     header is in MILLISECONDS. Tested with a freshly generated,
     throwaway 2048-bit RSA key — GUARDRAILS.md §1.3 forbids a real
     credential anywhere near a fixture, and the key id used everywhere
     here is the literal `"test-key-id"`.
  2. THE BIDS-ONLY BOOK. Both documented payload encodings
     (`orderbook_fp.*_dollars` strings and legacy `orderbook.yes/no`
     integer cents) produce an IDENTICAL `OrderBook` — the check that
     catches a missing `/100`; derived asks are exactly `1 - no_bid`;
     and the resulting book is UNCROSSED (`best_bid <= best_ask`), which
     is the direction proof. An inverted conversion would not raise
     anywhere: T07's fill engine merely declines a crossed book with
     `reason="crossed_book"`, so every Kalshi fill would go silently
     missing instead.
  3. FEE WAIVER PRECEDENCE. A market inside its
     `fee_waiver_expiration_time` window gets
     `FeeSchedule(0, 0, source="fee_waiver")`; an EXPIRED waiver falls
     through to the `Settings` rates with `source="settings"`.
  4. PAGINATION follows `cursor` until the venue stops handing one back.
  5. AUTH HEADERS are present on every request a credentialed adapter
     makes, public endpoints included.
  6. THE LIVE ADAPTER REFUSES to construct under default settings
     (GUARDRAILS.md §1.1/§1.2).
  7. THE ORDER BODY serializes `price` with <= 4 dp and `count` with
     <= 2 dp, and each of the four `(outcome, side)` routes goes to the
     endpoint the mapping table says it does.

All network access here is `httpx.MockTransport` against hand-written
fixtures under `tests/fixtures/kalshi/` (GUARDRAILS.md §1.4: no network
to `kalshi.com`/`kalshi.co`, ever, from a test).
"""
import base64
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.config import Settings
from app.execution.fences import LiveTradingDisabled
from app.venues.base import VenueAuthError, VenuePayloadError, VenueRateLimited
from app.venues.kalshi import auth as kalshi_auth
from app.venues.kalshi.adapter import KalshiAdapter, build_book
from app.venues.kalshi.live import (
    LEGACY_ORDERS_PATH,
    ORDER_ROUTES,
    V2_ORDERS_PATH,
    KalshiLiveAdapter,
    build_order_body,
)
from app.venues.types import OrderRequest

FIXTURES = Path(__file__).parent.parent / "fixtures" / "kalshi"


def _load(name: str) -> Any:
    with open(FIXTURES / name) as f:
        return json.load(f)


MARKETS: list[dict[str, Any]] = _load("markets.json")
MARKET: dict[str, Any] = _load("market.json")
ORDERBOOK_FP: dict[str, Any] = _load("orderbook_fp.json")
ORDERBOOK_CENTS: dict[str, Any] = _load("orderbook_legacy_cents.json")
BALANCE: dict[str, Any] = _load("balance.json")
ORDER_ACK: dict[str, Any] = _load("order_ack.json")

ALPHA = "TEST-26DEC31-ALPHA"
BETA = "TEST-26DEC31-BETA"
GAMMA = "TEST-25JAN01-GAMMA"
DELTA = "TEST-26NOV03-DELTA"

#: A throwaway RSA key, generated fresh in this process. Never written to
#: disk, never committed, and unrelated to any real Kalshi account
#: (GUARDRAILS.md §1.3).
TEST_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
TEST_PRIVATE_KEY_PEM = TEST_PRIVATE_KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("ascii")

#: The API key ID used in every test (GUARDRAILS.md §1.3).
TEST_KEY_ID = "test-key-id"

#: The demo base URL `kalshi_env="demo"` (the default) selects. Every
#: request below is intercepted by `httpx.MockTransport`; nothing is
#: sent anywhere.
DEMO_PATH_PREFIX = "/trade-api/v2"


def make_settings(credentialed: bool = True, **overrides: Any) -> Settings:
    """Build a `Settings` for a Kalshi adapter under test.

    Never live: `trading_mode` is left at its `"paper"` default unless a
    fence test explicitly overrides it (GUARDRAILS.md §1.2).
    """
    return Settings(
        KALSHI_API_KEY_ID=TEST_KEY_ID if credentialed else "",
        KALSHI_PRIVATE_KEY_PEM=TEST_PRIVATE_KEY_PEM if credentialed else "",
        **overrides,
    )


def make_transport(
    *,
    markets: list[dict[str, Any]] | None = None,
    market: dict[str, Any] | None = None,
    orderbook: dict[str, Any] | None = None,
    balance: dict[str, Any] | None = None,
    portfolio: dict[str, dict[str, Any]] | None = None,
    order_ack: dict[str, Any] | None = None,
    page_size: int = 2,
    recorder: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    """Route Kalshi requests to fixture data, paginating `/markets`.

    `/markets` serves `page_size` markets at a time and hands back a
    `cursor` until the list is exhausted, so `list_markets` exercises
    real cursor pagination rather than a single page.

    Args:
        markets: Market payloads for `/markets`. Defaults to the fixture.
        market: `{"market": ...}` envelope for `/markets/{ticker}`.
        orderbook: Response for `/markets/{ticker}/orderbook`.
        balance: Response for the balance endpoint.
        portfolio: Extra `{path_suffix: response}` portfolio routes, e.g.
            `{"positions": {...}}`.
        order_ack: Response for an order POST.
        page_size: Markets per `/markets` page.
        recorder: If given, every request is appended to it, so a test
            can assert on headers and query strings after the fact.
    """
    markets = MARKETS if markets is None else markets
    market = MARKET if market is None else market
    orderbook = ORDERBOOK_FP if orderbook is None else orderbook
    balance = BALANCE if balance is None else balance
    portfolio = {} if portfolio is None else portfolio
    order_ack = ORDER_ACK if order_ack is None else order_ack

    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(request)
        path = request.url.path.removeprefix(DEMO_PATH_PREFIX)
        if request.method == "POST" and path in (V2_ORDERS_PATH, LEGACY_ORDERS_PATH):
            return httpx.Response(201, json=order_ack)
        if path == "/markets":
            start = int(request.url.params.get("cursor") or 0)
            page = markets[start : start + page_size]
            nxt = start + page_size
            cursor = str(nxt) if nxt < len(markets) else ""
            return httpx.Response(200, json={"markets": page, "cursor": cursor})
        if path.endswith("/orderbook"):
            return httpx.Response(200, json=orderbook)
        if path.startswith("/markets/"):
            ticker = path.removeprefix("/markets/")
            wanted = market.get("market", {}).get("ticker")
            if ticker == wanted:
                return httpx.Response(200, json=market)
            match = next((m for m in markets if m.get("ticker") == ticker), None)
            if match is not None:
                return httpx.Response(200, json={"market": match})
            return httpx.Response(404, json={"error": "not found"})
        if path == "/portfolio/balance":
            return httpx.Response(200, json=balance)
        for suffix, body in portfolio.items():
            if path == f"/portfolio/{suffix}":
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": "unhandled", "path": path})

    return httpx.MockTransport(handler)


def make_adapter(
    *, credentialed: bool = True, settings_obj: Settings | None = None, **kwargs: Any
) -> KalshiAdapter:
    return KalshiAdapter(
        transport=make_transport(**kwargs),
        settings_obj=settings_obj or make_settings(credentialed=credentialed),
    )


def refusing_transport() -> httpx.MockTransport:
    """A transport that fails any test that unexpectedly reaches network I/O."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network call: {request.method} {request.url}")

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# 1. Auth: RSA-PSS signing, verified with the public key
# ---------------------------------------------------------------------------


def _verify(signature_b64: str, message: str) -> None:
    """Verify `signature_b64` over `message` with the test key's PUBLIC half.

    Salt length is asserted EXPLICITLY as the digest length (32 bytes for
    SHA-256) rather than with `padding.PSS.AUTO`: AUTO accepts any salt
    length, so it would not actually prove the pinned parameter
    (PLAN.md §3) is what the implementation used.
    """
    TEST_PRIVATE_KEY.public_key().verify(
        base64.b64decode(signature_b64),
        message.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=hashes.SHA256().digest_size,
        ),
        hashes.SHA256(),
    )


def test_sign_request_is_verifiable_with_the_public_key() -> None:
    """The signature verifies over `f"{ts}{METHOD}{path}"`, RSA-PSS/SHA256."""
    ts_ms = 1767225600000
    path = "/trade-api/v2/portfolio/balance"

    signature = kalshi_auth.sign_request(TEST_PRIVATE_KEY, ts_ms, "get", path)

    _verify(signature, f"{ts_ms}GET{path}")


def test_sign_request_strips_the_query_string() -> None:
    """A path with a query signs the same bytes as the path alone.

    PLAN.md §3: sign `/trade-api/v2/markets`, NOT `...?status=open`.
    Signing the query too yields a signature the venue cannot verify.
    """
    ts_ms = 1767225600000
    bare = "/trade-api/v2/markets"

    with_query = kalshi_auth.sign_request(
        TEST_PRIVATE_KEY, ts_ms, "GET", f"{bare}?status=open&limit=200"
    )

    # RSA-PSS is randomized (a fresh salt per signature), so two
    # signatures over the same bytes differ; equality of the SIGNATURES
    # would prove nothing. Verifying the query-carrying signature against
    # the QUERY-FREE message is the real assertion.
    _verify(with_query, f"{ts_ms}GET{bare}")


def test_sign_request_uppercases_the_method() -> None:
    """`"get"` and `"GET"` sign identical bytes."""
    ts_ms = 1767225600000
    path = "/trade-api/v2/markets"

    _verify(
        kalshi_auth.sign_request(TEST_PRIVATE_KEY, ts_ms, "get", path),
        f"{ts_ms}GET{path}",
    )


def test_auth_headers_timestamp_is_milliseconds() -> None:
    """`KALSHI-ACCESS-TIMESTAMP` is ms since the epoch, not seconds.

    A seconds-valued timestamp for 2026-09-05 is ~1.78e9; the ms value is
    ~1.78e12. The bound below (1e12) sits between the two by three orders
    of magnitude, so it cannot be satisfied by a seconds value until the
    year 33658.
    """
    headers = kalshi_auth.auth_headers(
        TEST_KEY_ID, TEST_PRIVATE_KEY, "GET", "/trade-api/v2/markets"
    )

    ts_ms = int(headers[kalshi_auth.ACCESS_TIMESTAMP_HEADER])
    assert ts_ms > 1_000_000_000_000
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    assert abs(now_ms - ts_ms) < 60_000  # within a minute of now


def test_auth_headers_signature_matches_its_own_timestamp() -> None:
    """The signature is over the exact timestamp the header carries."""
    url = "https://external-api.demo.kalshi.co/trade-api/v2/portfolio/fills?limit=10"

    headers = kalshi_auth.auth_headers(TEST_KEY_ID, TEST_PRIVATE_KEY, "GET", url)

    assert headers[kalshi_auth.ACCESS_KEY_HEADER] == TEST_KEY_ID
    ts = headers[kalshi_auth.ACCESS_TIMESTAMP_HEADER]
    _verify(
        headers[kalshi_auth.ACCESS_SIGNATURE_HEADER],
        f"{ts}GET/trade-api/v2/portfolio/fills",
    )


def test_load_private_key_rejects_empty_and_garbage_pem() -> None:
    """A missing or unparseable PEM is a `ValueError` carrying no key bytes."""
    with pytest.raises(ValueError):
        kalshi_auth.load_private_key("")
    with pytest.raises(ValueError):
        kalshi_auth.load_private_key("-----BEGIN PRIVATE KEY-----\nnope\n")


# ---------------------------------------------------------------------------
# 2. The bids-only book conversion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yes_book_derives_asks_from_no_bids() -> None:
    """YES asks are `1 - no_bid`, and the book is UNCROSSED.

    Fixture: yes bids 0.40x120, 0.39x300; no bids 0.58x80, 0.57x250.
    By hand:
      YES bids  = 0.40 x 120, 0.39 x 300            (as-is)
      YES asks  = 1 - 0.58 = 0.42 x 80,
                  1 - 0.57 = 0.43 x 250
      best_bid 0.40 <= best_ask 0.42                (uncrossed)
    """
    adapter = make_adapter()

    book = await adapter.get_book(ALPHA, "YES")

    assert book.venue == "kalshi"
    assert book.market_id == ALPHA
    assert book.outcome == "YES"
    assert [(lvl.price, lvl.size) for lvl in book.bids] == [
        pytest.approx((0.40, 120.0)),
        pytest.approx((0.39, 300.0)),
    ]
    assert [(lvl.price, lvl.size) for lvl in book.asks] == [
        pytest.approx((0.42, 80.0)),
        pytest.approx((0.43, 250.0)),
    ]
    best_bid, best_ask = book.best_bid(), book.best_ask()
    assert best_bid is not None and best_ask is not None
    assert best_bid.price <= best_ask.price
    assert book.depth_source == "recorded"


@pytest.mark.asyncio
async def test_no_book_is_the_mirror_image() -> None:
    """NO bids are the NO levels; NO asks are `1 - yes_bid`.

    By hand, from the same fixture:
      NO bids = 0.58 x 80, 0.57 x 250
      NO asks = 1 - 0.40 = 0.60 x 120, 1 - 0.39 = 0.61 x 300
      best_bid 0.58 <= best_ask 0.60                (uncrossed)
    """
    adapter = make_adapter()

    book = await adapter.get_book(ALPHA, "NO")

    assert [(lvl.price, lvl.size) for lvl in book.bids] == [
        pytest.approx((0.58, 80.0)),
        pytest.approx((0.57, 250.0)),
    ]
    assert [(lvl.price, lvl.size) for lvl in book.asks] == [
        pytest.approx((0.60, 120.0)),
        pytest.approx((0.61, 300.0)),
    ]
    best_bid, best_ask = book.best_bid(), book.best_ask()
    assert best_bid is not None and best_ask is not None
    assert best_bid.price <= best_ask.price


@pytest.mark.asyncio
async def test_the_two_outcome_books_tie_out_to_one_dollar() -> None:
    """YES bid + NO ask == 1.00 and YES ask + NO bid == 1.00.

    A YES and a NO contract together always pay exactly $1.00, so the
    complement relationship must hold exactly at the touch. If the
    subtraction were inverted anywhere, these sums would come out at
    0.80/1.20 rather than 1.00 -- and BOTH books would be crossed.
    """
    adapter = make_adapter()

    yes = await adapter.get_book(ALPHA, "YES")
    no = await adapter.get_book(ALPHA, "NO")

    yes_bid, yes_ask = yes.best_bid(), yes.best_ask()
    no_bid, no_ask = no.best_bid(), no.best_ask()
    assert yes_bid and yes_ask and no_bid and no_ask
    assert yes_bid.price + no_ask.price == pytest.approx(1.0)
    assert yes_ask.price + no_bid.price == pytest.approx(1.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["YES", "NO"])
async def test_both_payload_encodings_yield_identical_books(outcome: str) -> None:
    """`orderbook_fp` dollar strings and legacy integer cents agree exactly.

    This is the check that catches a missing `/100`: the cents fixture
    says `40`, the dollars fixture says `"0.40"`, and both must land on
    the probability 0.40. Only `ts` (the read time) is allowed to differ
    between the two reads.
    """
    fp_book = await make_adapter(orderbook=ORDERBOOK_FP).get_book(ALPHA, outcome)
    cents_book = await make_adapter(orderbook=ORDERBOOK_CENTS).get_book(ALPHA, outcome)

    assert replace(fp_book, ts=cents_book.ts) == cents_book


def test_build_book_rejects_an_unknown_outcome() -> None:
    """Only YES/NO exist on a Kalshi market."""
    with pytest.raises(VenuePayloadError):
        build_book(ORDERBOOK_FP, market_id=ALPHA, outcome="MAYBE", ts=datetime.now(UTC))


def test_build_book_rejects_a_payload_with_no_book() -> None:
    """Neither `orderbook_fp` nor `orderbook` present is an unknown shape."""
    with pytest.raises(VenuePayloadError):
        build_book({"nope": {}}, market_id=ALPHA, outcome="YES", ts=datetime.now(UTC))


def test_build_book_rejects_cents_masquerading_as_dollars() -> None:
    """`yes_dollars: [[40, 1]]` is 40 DOLLARS -- not a probability.

    This is the loud half of the units failure: a price above 1.0 is
    rejected by `BookLevel` and surfaces as `VenuePayloadError`.
    """
    with pytest.raises(VenuePayloadError):
        build_book(
            {"orderbook_fp": {"yes_dollars": [[40, 1]], "no_dollars": []}},
            market_id=ALPHA,
            outcome="YES",
            ts=datetime.now(UTC),
        )


def test_build_book_handles_an_empty_side() -> None:
    """A one-sided book is legal: no NO bids means no derived YES asks."""
    book = build_book(
        {"orderbook_fp": {"yes_dollars": [["0.40", 10]], "no_dollars": None}},
        market_id=ALPHA,
        outcome="YES",
        ts=datetime.now(UTC),
    )

    assert book.best_bid() is not None
    assert book.best_ask() is None


# ---------------------------------------------------------------------------
# 3. Market parsing and fee-waiver precedence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_markets_maps_every_documented_field() -> None:
    """`rules_primary`+`rules_secondary`, close/settle times, result, tick."""
    markets = await make_adapter().list_markets()
    by_id = {m.market_id: m for m in markets}

    alpha = by_id[ALPHA]
    assert alpha.venue == "kalshi"
    assert alpha.event_id == "TEST-26DEC31"
    assert alpha.question == "Will Team Alpha win the championship?"
    assert alpha.outcomes == ("YES", "NO")
    assert alpha.outcome_ids == {"YES": ALPHA, "NO": ALPHA}
    assert alpha.rules_text == (
        "This market resolves YES if Team Alpha officially wins the "
        "championship series.\n\nIf the series is cancelled without a "
        "winner, this market resolves NO."
    )
    assert alpha.close_time == datetime(2026, 12, 31, 23, 59, tzinfo=UTC)
    assert alpha.expected_settle_time == datetime(2027, 1, 1, 12, 0, tzinfo=UTC)
    assert alpha.status == "open"
    assert alpha.result is None
    # last_price_dollars "0.42" falls in the 0.05-0.95 band -> 0.01 tick.
    assert alpha.tick_size == pytest.approx(0.01)

    beta = by_id[BETA]
    # last_price_dollars "0.03" falls in the 0.00-0.05 band -> 0.001 tick.
    assert beta.tick_size == pytest.approx(0.001)
    assert beta.rules_text.endswith("first round.")  # empty rules_secondary

    gamma = by_id[GAMMA]
    assert gamma.status == "resolved"
    assert gamma.result == "yes"
    assert gamma.tick_size == pytest.approx(0.01)  # no price_level_structure


@pytest.mark.asyncio
async def test_active_fee_waiver_wins_over_settings_rates() -> None:
    """BETA's waiver runs to 2099 -> zero rates, `source="fee_waiver"`.

    The SOURCE STRING is behavioural, not cosmetic: T07's fill engine
    accepts a zero taker rate silently only from `fee_waiver`,
    `category_table`, or `clob_market`. Any other spelling and a genuine
    Kalshi waiver is flagged as a suspicious free fill.
    """
    markets = await make_adapter().list_markets()
    by_id = {m.market_id: m for m in markets}

    assert by_id[BETA].fee.source == "fee_waiver"
    assert by_id[BETA].fee.taker_rate == 0.0
    assert by_id[BETA].fee.maker_rate == 0.0


@pytest.mark.asyncio
async def test_expired_fee_waiver_falls_back_to_settings_rates() -> None:
    """ALPHA's waiver expired in 2020 -> the `Settings` rates apply.

    A waiver that ended is not a waiver. `KALSHI_TAKER_FEE_RATE`
    defaults to 0.07 (PLAN.md §3).
    """
    markets = await make_adapter().list_markets()
    by_id = {m.market_id: m for m in markets}

    assert by_id[ALPHA].fee.source == "settings"
    assert by_id[ALPHA].fee.taker_rate == pytest.approx(0.07)
    assert by_id[ALPHA].fee.maker_rate == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_settings_fee_rate_override_reaches_the_schedule() -> None:
    """The rate is never a literal: a `Settings` override changes it."""
    settings_obj = make_settings(KALSHI_TAKER_FEE_RATE=0.02)
    adapter = KalshiAdapter(transport=make_transport(), settings_obj=settings_obj)

    market = await adapter.get_market(ALPHA)

    assert market.fee.taker_rate == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_get_market_parses_a_legacy_integer_cents_payload() -> None:
    """A market quoting bare `last_price: 42` means 42 CENTS -> 0.42.

    The cents-vs-dollars funnel applies to market payloads too, not just
    books: the DELTA fixture uses the legacy encoding throughout,
    including a cents-keyed `price_level_structure` (`tick_size: 1`, one
    cent).
    """
    market = await make_adapter().get_market(DELTA)

    assert market.market_id == DELTA
    assert market.tick_size == pytest.approx(0.01)  # 1 cent band at 0.42
    assert market.min_size == pytest.approx(1.0)
    assert market.close_time == datetime(2026, 11, 3, 23, 59, tzinfo=UTC)


@pytest.mark.asyncio
async def test_get_market_missing_market_object_raises_payload_error() -> None:
    """An unknown ticker (HTTP 404) never yields a half-built market."""
    adapter = make_adapter()

    with pytest.raises((VenuePayloadError, httpx.HTTPStatusError)):
        await adapter.get_market("TEST-NO-SUCH-TICKER")


@pytest.mark.asyncio
async def test_market_missing_close_time_is_a_payload_error() -> None:
    """`close_time` has no safe default -- time-to-resolution depends on it."""
    broken = {k: v for k, v in MARKETS[0].items() if k != "close_time"}
    adapter = make_adapter(markets=[broken])

    with pytest.raises(VenuePayloadError):
        await adapter.list_markets()


# ---------------------------------------------------------------------------
# 4. Cursor pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_markets_follows_the_cursor_across_pages() -> None:
    """Three fixture markets served two at a time -> two requests, all three."""
    recorder: list[httpx.Request] = []
    adapter = make_adapter(page_size=2, recorder=recorder)

    markets = await adapter.list_markets()

    assert {m.market_id for m in markets} == {ALPHA, BETA, GAMMA}
    market_calls = [r for r in recorder if r.url.path.endswith("/markets")]
    assert len(market_calls) == 2
    assert market_calls[0].url.params.get("cursor") is None
    assert market_calls[1].url.params["cursor"] == "2"
    assert market_calls[0].url.params["limit"] == "200"


@pytest.mark.asyncio
async def test_list_markets_sends_and_applies_the_status_filter() -> None:
    """`status="resolved"` is sent as Kalshi's `settled` AND re-checked locally."""
    recorder: list[httpx.Request] = []
    adapter = make_adapter(recorder=recorder)

    markets = await adapter.list_markets(status="resolved")

    assert {m.market_id for m in markets} == {GAMMA}
    assert recorder[0].url.params["status"] == "settled"


@pytest.mark.asyncio
async def test_list_markets_updated_since_keeps_markets_with_no_update_time() -> None:
    """An unknown update time is not evidence of staleness."""
    no_ts = [{k: v for k, v in m.items() if k != "last_update_ts"} for m in MARKETS]
    adapter = make_adapter(markets=no_ts)

    markets = await adapter.list_markets(
        updated_since=datetime(2030, 1, 1, tzinfo=UTC)
    )

    assert len(markets) == 3


# ---------------------------------------------------------------------------
# 5. Auth headers on every request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_request_carries_verifiable_auth_headers() -> None:
    """Public and credentialed endpoints alike are signed when credentialed.

    Each captured request's signature is re-verified against ITS OWN
    path and timestamp, so this proves the signed path tracks the path
    actually sent (query string excluded) rather than merely that three
    headers exist.
    """
    recorder: list[httpx.Request] = []
    adapter = make_adapter(recorder=recorder)

    await adapter.list_markets()
    await adapter.get_market(ALPHA)
    await adapter.get_book(ALPHA, "YES")
    await adapter.get_balance()

    assert len(recorder) >= 5
    for request in recorder:
        headers = request.headers
        assert headers[kalshi_auth.ACCESS_KEY_HEADER] == TEST_KEY_ID
        ts = headers[kalshi_auth.ACCESS_TIMESTAMP_HEADER]
        _verify(
            headers[kalshi_auth.ACCESS_SIGNATURE_HEADER],
            f"{ts}{request.method}{request.url.path}",
        )
        assert request.url.path.startswith(DEMO_PATH_PREFIX)


@pytest.mark.asyncio
async def test_public_endpoints_work_without_credentials() -> None:
    """Market data does not require a key; it is simply sent unsigned."""
    recorder: list[httpx.Request] = []
    adapter = make_adapter(credentialed=False, recorder=recorder)

    book = await adapter.get_book(ALPHA, "YES")

    assert book.best_bid() is not None
    assert kalshi_auth.ACCESS_SIGNATURE_HEADER not in recorder[0].headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name", ["get_balance", "get_positions", "get_open_orders"]
)
async def test_portfolio_endpoints_require_credentials(method_name: str) -> None:
    """Uncredentialed portfolio reads raise before touching the network."""
    adapter = KalshiAdapter(
        transport=refusing_transport(), settings_obj=make_settings(credentialed=False)
    )

    with pytest.raises(VenueAuthError):
        await getattr(adapter, method_name)()


@pytest.mark.asyncio
async def test_get_fills_requires_credentials() -> None:
    """`get_fills` takes a `since`; the same credential gate applies."""
    adapter = KalshiAdapter(
        transport=refusing_transport(), settings_obj=make_settings(credentialed=False)
    )

    with pytest.raises(VenueAuthError):
        await adapter.get_fills(datetime(2026, 1, 1, tzinfo=UTC))


@pytest.mark.asyncio
async def test_an_unusable_pem_is_an_auth_error_not_a_value_error() -> None:
    """A malformed key is a credential fault, surfaced as `VenueAuthError`."""
    settings_obj = Settings(
        KALSHI_API_KEY_ID=TEST_KEY_ID, KALSHI_PRIVATE_KEY_PEM="not-a-pem"
    )
    adapter = KalshiAdapter(transport=refusing_transport(), settings_obj=settings_obj)

    with pytest.raises(VenueAuthError):
        await adapter.get_balance()


# ---------------------------------------------------------------------------
# Balance / positions / fills: both encodings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_balance_reads_legacy_integer_cents() -> None:
    """`{"balance": 123456}` is 123456 CENTS = $1234.56."""
    adapter = make_adapter()

    balance = await adapter.get_balance()

    assert balance.venue == "kalshi"
    assert balance.available == pytest.approx(1234.56)
    assert balance.locked == 0.0


@pytest.mark.asyncio
async def test_balance_reads_fixed_point_dollar_strings() -> None:
    """`{"balance_dollars": "1234.56"}` is already dollars -- no /100."""
    adapter = make_adapter(balance={"balance_dollars": "1234.56"})

    balance = await adapter.get_balance()

    assert balance.available == pytest.approx(1234.56)


@pytest.mark.asyncio
async def test_balance_missing_field_is_a_payload_error() -> None:
    adapter = make_adapter(balance={"unexpected": 1})

    with pytest.raises(VenuePayloadError):
        await adapter.get_balance()


@pytest.mark.asyncio
async def test_positions_split_a_signed_contract_count_into_outcome_and_size() -> None:
    """Positive `position` is YES, negative is NO; size is always >= 0.

    By hand: 120 contracts costing 4800 cents = $48.00 -> avg 0.40/contract.
    And -80 NO contracts costing 4640 cents = $46.40 -> avg 0.58/contract.
    """
    adapter = make_adapter(
        portfolio={
            "positions": {
                "market_positions": [
                    {"ticker": ALPHA, "position": 120, "market_exposure": 4800},
                    {"ticker": BETA, "position": -80, "market_exposure": 4640},
                    {"ticker": GAMMA, "position": 0, "market_exposure": 0},
                ]
            }
        }
    )

    positions = await adapter.get_positions()

    by_id = {p.market_id: p for p in positions}
    assert set(by_id) == {ALPHA, BETA}  # the flat position is dropped
    assert by_id[ALPHA].outcome == "YES"
    assert by_id[ALPHA].size == pytest.approx(120.0)
    assert by_id[ALPHA].avg_price == pytest.approx(0.40)
    assert by_id[BETA].outcome == "NO"
    assert by_id[BETA].size == pytest.approx(80.0)
    assert by_id[BETA].avg_price == pytest.approx(0.58)


@pytest.mark.asyncio
async def test_fills_price_the_side_that_actually_traded() -> None:
    """A NO fill is priced as NO (0.58), not as its YES complement (0.42).

    The venue-reported fee is used when present. Where it is absent the
    fee is ESTIMATED rather than recorded as $0.00 -- a fabricated zero
    fee is what makes a marginal edge look profitable.
    """
    adapter = make_adapter(
        portfolio={
            "fills": {
                "fills": [
                    {
                        "trade_id": "t1",
                        "order_id": "o1",
                        "ticker": ALPHA,
                        "side": "no",
                        "action": "buy",
                        "count": 10,
                        "no_price": 58,
                        "yes_price": 42,
                        "fee_paid": 3,
                        "is_taker": True,
                        "created_time": "2026-06-01T12:00:00Z",
                    },
                    {
                        "trade_id": "t2",
                        "order_id": "o2",
                        "ticker": ALPHA,
                        "side": "yes",
                        "count": 5,
                        "yes_price_dollars": "0.40",
                        "is_taker": False,
                        "created_time": "2025-01-01T00:00:00Z",
                    },
                ]
            }
        }
    )

    fills = await adapter.get_fills(datetime(2026, 1, 1, tzinfo=UTC))

    assert len(fills) == 1  # the 2025 fill is before `since`
    fill = fills[0]
    assert fill.price == pytest.approx(0.58)
    assert fill.size == pytest.approx(10.0)
    assert fill.fee == pytest.approx(0.03)  # 3 cents, reported by the venue
    assert fill.metadata["fee_source"] == "venue"
    assert fill.metadata["outcome"] == "NO"


@pytest.mark.asyncio
async def test_a_fill_with_no_reported_fee_is_estimated_not_zeroed() -> None:
    """No `fee_paid` -> the `Settings` rate is applied and labeled.

    By hand, Kalshi's formula at the default 0.07 taker rate:
    10 x 0.07 x 0.40 x 0.60 = 0.168, ceiled to 6dp then up to the whole
    cent -> $0.17.
    """
    adapter = make_adapter(
        portfolio={
            "fills": {
                "fills": [
                    {
                        "order_id": "o3",
                        "ticker": ALPHA,
                        "side": "yes",
                        "count": 10,
                        "yes_price_dollars": "0.40",
                        "is_taker": True,
                        "created_time": "2026-06-01T12:00:00Z",
                    }
                ]
            }
        }
    )

    fills = await adapter.get_fills(datetime(2026, 1, 1, tzinfo=UTC))

    assert fills[0].fee == pytest.approx(0.17)
    assert fills[0].metadata["fee_source"] == "settings_estimate"


@pytest.mark.asyncio
async def test_open_orders_normalize_resting_orders() -> None:
    """`GET`ting resting orders is a read; it places nothing."""
    adapter = make_adapter(portfolio={"orders": {"orders": [ORDER_ACK["order"]]}})

    orders = await adapter.get_open_orders()

    assert len(orders) == 1
    ack = orders[0]
    assert ack.venue == "kalshi"
    assert ack.order_id == "b0000000-0000-4000-8000-000000000001"
    assert ack.client_order_id == "intent-0001:0:0"
    assert ack.status == "open"  # "resting"
    assert ack.filled_size == pytest.approx(40.0)
    assert ack.remaining_size == pytest.approx(60.0)
    assert ack.avg_fill_price == pytest.approx(0.42)
    assert ack.ts == datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_order_ack_average_price_in_legacy_cents() -> None:
    """`average_fill_price: 42` is 42 CENTS -> 0.42, not 42.0."""
    raw = dict(ORDER_ACK["order"])
    del raw["average_fill_price_dollars"]
    raw["average_fill_price"] = 42
    adapter = make_adapter(portfolio={"orders": {"orders": [raw]}})

    orders = await adapter.get_open_orders()

    assert orders[0].avg_fill_price == pytest.approx(0.42)


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_auth_status_codes_map_to_venue_auth_error(status_code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "denied"})

    adapter = KalshiAdapter(
        transport=httpx.MockTransport(handler), settings_obj=make_settings()
    )

    with pytest.raises(VenueAuthError):
        await adapter.list_markets()


@pytest.mark.asyncio
async def test_429_maps_to_venue_rate_limited_with_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "2.5"}, json={})

    adapter = KalshiAdapter(
        transport=httpx.MockTransport(handler), settings_obj=make_settings()
    )

    with pytest.raises(VenueRateLimited) as excinfo:
        await adapter.list_markets()
    assert excinfo.value.retry_after_s == pytest.approx(2.5)


@pytest.mark.asyncio
async def test_a_non_object_body_is_a_payload_error_carrying_the_body() -> None:
    """`VenuePayloadError(raw=body)` -- the offending payload travels along."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "an", "object"])

    adapter = KalshiAdapter(
        transport=httpx.MockTransport(handler), settings_obj=make_settings()
    )

    with pytest.raises(VenuePayloadError) as excinfo:
        await adapter.list_markets()
    assert excinfo.value.raw == ["not", "an", "object"]


# ---------------------------------------------------------------------------
# 6. The live fence
# ---------------------------------------------------------------------------


def test_kalshi_live_adapter_disabled_by_default_settings() -> None:
    """Default (paper) settings refuse construction outright."""
    with pytest.raises(LiveTradingDisabled):
        KalshiLiveAdapter(transport=refusing_transport())


def test_kalshi_live_adapter_disabled_with_mode_but_no_confirmation() -> None:
    """`trading_mode="live"` alone is not enough."""
    mode_only = Settings(trading_mode="live", live_trading_confirmation="")

    # The field-name spelling MUST take effect (see
    # `tests/test_settings_aliases.py`): if it were silently ignored this
    # object would be paper-mode and the test would pass vacuously.
    assert mode_only.trading_mode == "live"
    with pytest.raises(LiveTradingDisabled):
        KalshiLiveAdapter(transport=refusing_transport(), settings_obj=mode_only)


def test_kalshi_live_adapter_disabled_with_confirmation_but_paper_mode() -> None:
    """The confirmation phrase alone is not enough either."""
    confirmation_only = Settings(
        trading_mode="paper", live_trading_confirmation="I_UNDERSTAND_REAL_MONEY"
    )

    with pytest.raises(LiveTradingDisabled):
        KalshiLiveAdapter(
            transport=refusing_transport(), settings_obj=confirmation_only
        )


def _permissive_settings() -> Settings:
    """An explicit, in-memory `Settings` with the fence deliberately open.

    GUARDRAILS.md §1.2 pattern: never the environment, never `.env`,
    never a value that reaches the registry. Constructing a live adapter
    from this places no order by itself; every request below is still
    intercepted by `httpx.MockTransport`.
    """
    return Settings(
        trading_mode="live",
        live_trading_confirmation="I_UNDERSTAND_REAL_MONEY",
        KALSHI_API_KEY_ID=TEST_KEY_ID,
        KALSHI_PRIVATE_KEY_PEM=TEST_PRIVATE_KEY_PEM,
    )


def test_kalshi_live_adapter_constructs_with_explicit_permissive_settings() -> None:
    """The fence CAN open -- with both conditions set, explicitly."""
    adapter = KalshiLiveAdapter(
        transport=refusing_transport(), settings_obj=_permissive_settings()
    )

    assert adapter.venue == "kalshi"


# ---------------------------------------------------------------------------
# 7. The order-body mapping table -- one case per route
# ---------------------------------------------------------------------------


def _order(outcome: str, side: str, **kwargs: Any) -> OrderRequest:
    defaults: dict[str, Any] = {
        "venue": "kalshi",
        "market_id": ALPHA,
        "outcome": outcome,
        "side": side,
        "price": 0.42,
        "size": 100.0,
        "tif": "GTC",
        "client_order_id": "intent-0001:0:0",
    }
    defaults.update(kwargs)
    return OrderRequest(**defaults)


@pytest.mark.parametrize(
    ("outcome", "side", "expected_path", "expected_side", "expected_action"),
    [
        ("YES", "BUY", V2_ORDERS_PATH, "bid", None),
        ("YES", "SELL", V2_ORDERS_PATH, "ask", None),
        ("NO", "BUY", LEGACY_ORDERS_PATH, "no", "buy"),
        ("NO", "SELL", LEGACY_ORDERS_PATH, "no", "sell"),
    ],
)
def test_each_direction_routes_to_its_documented_endpoint(
    outcome: str,
    side: str,
    expected_path: str,
    expected_side: str,
    expected_action: str | None,
) -> None:
    """One case per row of `ORDER_ROUTES` (module docstring in live.py).

    The YES rows are backed by the PLAN.md §3 vendor pin. The NO rows use
    the LEGACY endpoint and are explicitly marked UNVERIFIED there --
    this test pins the behaviour so a later doc check has something
    concrete to confirm or correct.
    """
    route = ORDER_ROUTES[(outcome, side)]

    assert route.path == expected_path
    assert route.body_side == expected_side
    assert route.action == expected_action
    assert route.verified is (outcome == "YES")

    body = build_order_body(_order(outcome, side), route)
    assert body["side"] == expected_side
    assert body.get("action") == expected_action
    assert body["ticker"] == ALPHA
    assert body["client_order_id"] == "intent-0001:0:0"


def test_order_body_serializes_price_to_at_most_four_decimals() -> None:
    """`price` is a fixed-point dollar string, <= 4 dp (PLAN.md §3).

    0.42 must not arrive as `0.42000000000000004`, the double a derived
    NO price (`1 - 0.58`) actually produces.
    """
    body = build_order_body(
        _order("YES", "BUY", price=1.0 - 0.58), ORDER_ROUTES[("YES", "BUY")]
    )

    assert body["price"] == "0.4200"
    assert len(body["price"].split(".")[1]) <= 4


def test_order_body_serializes_count_to_at_most_two_decimals() -> None:
    """`count` is a fixed-point string, <= 2 dp."""
    body = build_order_body(
        _order("YES", "BUY", size=100.0), ORDER_ROUTES[("YES", "BUY")]
    )

    assert body["count"] == "100.00"
    assert len(body["count"].split(".")[1]) <= 2


def test_order_body_maps_every_time_in_force() -> None:
    """GTC/IOC/FOK -> Kalshi's own vocabulary (PLAN.md §3)."""
    route = ORDER_ROUTES[("YES", "BUY")]
    expected = {
        "GTC": "good_till_canceled",
        "IOC": "immediate_or_cancel",
        "FOK": "fill_or_kill",
    }

    for tif, kalshi_value in expected.items():
        body = build_order_body(_order("YES", "BUY", tif=tif), route)
        assert body["time_in_force"] == kalshi_value


def test_order_body_omits_post_only_unless_requested() -> None:
    """`post_only` is only emitted when actually asked for.

    The legacy endpoint's field set is unverified, so a `False` it might
    not understand is never sent.
    """
    route = ORDER_ROUTES[("NO", "BUY")]

    assert "post_only" not in build_order_body(_order("NO", "BUY"), route)
    assert build_order_body(_order("NO", "BUY", post_only=True), route)["post_only"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_path"),
    [("YES", V2_ORDERS_PATH), ("NO", LEGACY_ORDERS_PATH)],
)
async def test_place_order_posts_to_the_routed_endpoint(
    outcome: str, expected_path: str
) -> None:
    """End to end against `MockTransport` -- no real order is ever placed."""
    recorder: list[httpx.Request] = []
    adapter = KalshiLiveAdapter(
        transport=make_transport(recorder=recorder),
        settings_obj=_permissive_settings(),
    )

    ack = await adapter.place_order(_order(outcome, "BUY"))

    posts = [r for r in recorder if r.method == "POST"]
    assert len(posts) == 1
    assert posts[0].url.path == f"{DEMO_PATH_PREFIX}{expected_path}"
    assert kalshi_auth.ACCESS_SIGNATURE_HEADER in posts[0].headers
    assert ack.venue == "kalshi"
    assert ack.client_order_id == "intent-0001:0:0"
    assert ack.avg_fill_price == pytest.approx(0.42)


@pytest.mark.asyncio
async def test_place_order_refuses_an_unroutable_direction() -> None:
    """An outcome that is neither YES nor NO is refused, not guessed at."""
    adapter = KalshiLiveAdapter(
        transport=refusing_transport(), settings_obj=_permissive_settings()
    )
    order = OrderRequest(
        venue="kalshi",
        market_id=ALPHA,
        outcome="MAYBE",
        side="BUY",
        price=0.42,
        size=10.0,
        tif="GTC",
        client_order_id="intent-0002:0:0",
    )

    with pytest.raises(VenuePayloadError):
        await adapter.place_order(order)


def test_the_read_adapter_cannot_place_or_cancel_orders() -> None:
    """`KalshiAdapter` has no write path at all (GUARDRAILS.md §1.1).

    This is the structural half of the fence, and it is stronger than
    the acceptance grep: the read adapter exposes no `place_order` /
    `cancel_order` and its only HTTP verb is GET, so no amount of text
    matching (or evading it) changes what it can do.
    """
    assert not hasattr(KalshiAdapter, "place_order")
    assert not hasattr(KalshiAdapter, "cancel_order")
    assert hasattr(KalshiLiveAdapter, "place_order")


def test_registry_registers_kalshi_live_behind_the_fence() -> None:
    """`get_adapter("kalshi", "live")` exists and is fenced by default."""
    from app.venues.registry import get_adapter

    with pytest.raises(LiveTradingDisabled):
        get_adapter("kalshi", "live")
