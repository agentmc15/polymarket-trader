"""`get_book` must resolve an outcome the way the rest of the kit spells it.

THE ASYMMETRY. `KalshiAdapter.get_book` documents its `outcome` argument
as `"YES"` or `"NO"`, CASE-INSENSITIVE. `PolymarketAdapter.get_book`
resolved it with an exact dict lookup into `market.outcome_ids`, and
Gamma spells its outcomes `"Yes"`/`"No"` in title case. So the same
canonical argument reached one venue and bounced off the other:

    await kalshi.get_book(mid, "YES")      -> a book
    await polymarket.get_book(mid, "YES")  -> VenuePayloadError

That matters because of who calls it. `app.strategies.base.
normalize_outcome` says it outright — "every strategy in this kit
hardcodes uppercase" — and `outcome_key` exists precisely because
resolving an outcome and keying on one had drifted apart before. T21d
recorded that drift as a money bug in the same words this defect
reproduces: a position keyed `polymarket:M:TRUMP` against a payload's
`"Trump"` marked at its entry price forever.

AND THE FAILURE IS SILENT. `VenuePayloadError` is a `VenueError`, so it
is inside `scanner.VENUE_READ_FAULTS` — the containment set that exists
so one bad book costs one book. A caller passing the canonical spelling
therefore does not get an error. It gets a `debug`-level log line and a
skipped book, which reads downstream as "no opportunity here". Kalshi
books would keep arriving and Polymarket's would quietly stop, and the
scan would report a smaller number with no indication why.

The scanner is not currently affected — it iterates `market.outcomes`
and so passes each venue its own spelling — which is exactly what makes
this worth pinning rather than leaving: nothing fails today, the trap is
armed for the next caller, and this repo has now shipped three
strategies that were structurally unreachable on live data without a
single test noticing.

Exact matches still win, so a genuine multi-outcome label is untouched:
case-folding is a fallback for the binary pair, never a rewrite of what
a venue chose to call an outcome.
"""
import httpx
import pytest

from app.venues.base import VenuePayloadError
from app.venues.polymarket.adapter import PolymarketAdapter

_CONDITION = "0x" + "c" * 64
_YES_TOKEN = "111"
_NO_TOKEN = "222"


def _gamma_market(outcomes: str = '["Yes", "No"]') -> dict:
    return {
        "id": "424242",
        "conditionId": _CONDITION,
        "question": "Will it?",
        "outcomes": outcomes,
        "outcomePrices": '["0.4", "0.6"]',
        "clobTokenIds": f'["{_YES_TOKEN}", "{_NO_TOKEN}"]',
        "endDate": "2027-01-01T00:00:00Z",
        "active": True,
        "closed": False,
    }


def _transport(outcomes: str = '["Yes", "No"]') -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "clob" in str(request.url.host):
            if path.startswith("/markets/"):
                # The CLOB enrichment leg: tick/min size come from here,
                # and the adapter refuses to invent a tick size.
                return httpx.Response(
                    200,
                    json={
                        "condition_id": _CONDITION,
                        "minimum_tick_size": "0.001",
                        "minimum_order_size": "5",
                    },
                )
            if path.endswith("/book"):
                token = request.url.params.get("token_id")
                price = "0.40" if token == _YES_TOKEN else "0.60"
                return httpx.Response(
                    200,
                    json={
                        "timestamp": "1789200000000",
                        "bids": [{"price": price, "size": "100"}],
                        "asks": [{"price": price, "size": "100"}],
                    },
                )
            return httpx.Response(200, json={"data": []})
        if path.endswith("/markets"):
            offset = request.url.params.get("offset", "0")
            return httpx.Response(
                200, json=[_gamma_market(outcomes)] if offset == "0" else []
            )
        if path.endswith("/events"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={})

    return httpx.MockTransport(handle)


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["Yes", "YES", "yes", " YES "])
async def test_the_canonical_spelling_resolves_like_kalshis_does(spelling: str) -> None:
    """One argument, both venues — the contract Kalshi already documents."""
    adapter = PolymarketAdapter(transport=_transport())

    book = await adapter.get_book(_CONDITION, spelling)

    assert book.best_ask() is not None
    assert book.best_ask().price == pytest.approx(0.40)


@pytest.mark.asyncio
async def test_an_exact_match_still_wins() -> None:
    """Case-folding is a fallback, never a rewrite of the venue's label.

    Two outcomes differing only in case are a venue's business, not this
    adapter's to merge — the exact spelling must still address exactly
    the token the venue paired with it.
    """
    adapter = PolymarketAdapter(transport=_transport('["Trump", "TRUMP"]'))

    first = await adapter.get_book(_CONDITION, "Trump")
    second = await adapter.get_book(_CONDITION, "TRUMP")

    assert first.best_ask().price == pytest.approx(0.40)
    assert second.best_ask().price == pytest.approx(0.60)


@pytest.mark.asyncio
async def test_an_ambiguous_fold_still_raises() -> None:
    """When only case tells two outcomes apart, guessing is not allowed."""
    adapter = PolymarketAdapter(transport=_transport('["Trump", "TRUMP"]'))

    with pytest.raises(VenuePayloadError, match="ambiguous"):
        await adapter.get_book(_CONDITION, "trump")


@pytest.mark.asyncio
async def test_a_genuinely_unknown_outcome_still_raises() -> None:
    adapter = PolymarketAdapter(transport=_transport())

    with pytest.raises(VenuePayloadError, match="unknown outcome"):
        await adapter.get_book(_CONDITION, "MAYBE")
