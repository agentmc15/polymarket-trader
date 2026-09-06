"""Check the CLOB book shape against Polymarket's OWN client library.

GUARDRAILS.md §1.4 bars contacting a venue, so every other test in this
repo validates the adapter against a fixture this repo wrote. That
proves the adapter parses what we THINK Polymarket sends. It cannot
detect the case that actually matters: that what we think is wrong.

`py-clob-client` is Polymarket's official client and is already a
declared dependency (`requirements.txt`). Its `OrderBookSummary` /
`OrderSummary` dataclasses are the vendor's own statement of the
`/book` payload -- field names and types both. Reading the schema off
the vendor's package is not the same as seeing a live response, but it
is evidence from Polymarket rather than from us, and it costs no
network.

What this buys that a fixture cannot: when the dependency is upgraded
and a field is renamed or retyped, these fail. A hand-written fixture
simply keeps agreeing with itself forever.
"""
from dataclasses import fields
from typing import Any, get_type_hints

import pytest
from py_clob_client.clob_types import OrderBookSummary, OrderSummary

from tests.venues.test_polymarket_adapter import CLOB_BOOK, MARKET_A001, _adapter

_BOOK_FIELDS = {f.name for f in fields(OrderBookSummary)}
_LEVEL_FIELDS = {f.name for f in fields(OrderSummary)}

#: Read by `PolymarketAdapter._book_from_payload`. `last_trade_price` is
#: deliberately absent: the adapter reads it with `.get()` into metadata
#: and the vendor does NOT model it, so requiring it would assert the
#: vendor library is exhaustive, which it is not.
_BOOK_KEYS_WE_READ = {
    "market",
    "asset_id",
    "timestamp",
    "bids",
    "asks",
    "min_order_size",
    "tick_size",
    "neg_risk",
    "hash",
}


def test_every_book_field_we_read_exists_in_the_vendor_schema() -> None:
    """A field we read that the vendor does not send is a live failure."""
    missing = _BOOK_KEYS_WE_READ - _BOOK_FIELDS

    assert not missing, (
        f"adapter reads {sorted(missing)} from the CLOB book, but "
        f"py_clob_client.OrderBookSummary declares {sorted(_BOOK_FIELDS)}"
    )


def test_level_fields_match_the_vendor_schema() -> None:
    assert {"price", "size"} <= _LEVEL_FIELDS


@pytest.mark.parametrize(
    "field_name", ["timestamp", "min_order_size", "tick_size"]
)
def test_vendor_sends_these_as_strings_and_our_fixture_agrees(field_name: str) -> None:
    """The venue sends decimal STRINGS, not numbers.

    If a fixture used real numbers here it would be testing a friendlier
    payload than the one that arrives, and the string path -- the only
    one that runs in production -- would be unexercised.
    """
    assert get_type_hints(OrderBookSummary)[field_name] is str
    assert isinstance(CLOB_BOOK[field_name], str)


@pytest.mark.parametrize("field_name", ["price", "size"])
def test_vendor_sends_level_numbers_as_strings_and_our_fixture_agrees(
    field_name: str,
) -> None:
    assert get_type_hints(OrderSummary)[field_name] is str
    for side in ("bids", "asks"):
        for level in CLOB_BOOK[side]:
            assert isinstance(level[field_name], str), (side, level)


@pytest.mark.asyncio
async def test_a_book_built_from_the_vendor_dataclass_parses() -> None:
    """End to end on a payload constructed via Polymarket's own types.

    Built by instantiating the vendor dataclasses and serialising them,
    rather than by copying our fixture -- so the shape under test comes
    from the vendor package.
    """
    summary = OrderBookSummary(
        market=CLOB_BOOK["market"],
        asset_id=CLOB_BOOK["asset_id"],
        timestamp="1767225600000",
        bids=[OrderSummary(price="0.40", size="120.5")],
        asks=[OrderSummary(price="0.42", size="80")],
        min_order_size="5",
        neg_risk=False,
        tick_size="0.01",
        hash="0x00000000000000000000000000000000000000000000000000000000000abc",
    )
    payload: dict[str, Any] = {
        f.name: getattr(summary, f.name) for f in fields(OrderBookSummary)
    }
    payload["bids"] = [{"price": lvl.price, "size": lvl.size} for lvl in summary.bids]
    payload["asks"] = [{"price": lvl.price, "size": lvl.size} for lvl in summary.asks]

    book = await _adapter(clob_book=payload).get_book(MARKET_A001, "Yes")

    assert [(lvl.price, lvl.size) for lvl in book.bids] == [(0.40, 120.5)]
    assert [(lvl.price, lvl.size) for lvl in book.asks] == [(0.42, 80.0)]
