"""Polymarket publishes each market's fee rate. Use it.

MEASURED ON LIVE DATA: the adapter assigned **0.05 to all 1,918 open
markets** while the venue published four different rates in a payload the
adapter already fetches:

    venue rate   markets   what we used   direction
    0.04         954       0.05           overstated
    0.07         361       0.05           UNDERSTATED
    0.03         267       0.05           overstated
    0.05         194       0.05           correct
    fees off     142       0.05           charged where the venue does not

**1,582 of 1,918 markets — 82% — carried the wrong fee.** Understating
is the dangerous direction: 361 crypto markets were priced at 0.05
against a real 0.07, so every crypto edge this repo computed was
overstated by 2% of `p(1-p)` per contract.

Every Gamma market carries `feesEnabled` and `feeType`, and 1,776 of
1,918 carry a `feeSchedule` object: `{"exponent": 1, "rate": 0.04,
"takerOnly": true, "rebateRate": 0.25}`. That `rate` is the venue's own
per-market answer and outranks any table we maintain by hand — the
category table has to guess from a category string that is frequently
absent, which is exactly why every market fell through to the 0.05
unknown-category fallback.

`exponent: 1` matches `PolymarketFeeModel`'s `rate * p * (1 - p)`, so an
exponent the model cannot express is a reason to REFUSE the published
schedule and fall back, not to apply its rate under the wrong formula.

`takerOnly: true` on all 1,776 confirms the model's standing assumption
that Polymarket makers pay nothing. `rebateRate` (0.25 on 1,415 markets,
0.20 on 361) is NOT modelled here: a rebate paid to makers would make
this a materially better venue to quote on than Kalshi, and guessing at
its mechanics would be inventing revenue.
"""
import httpx
import pytest

from app.venues.polymarket.adapter import PolymarketAdapter

_CONDITION = "0x" + "e" * 64


def _market(**overrides) -> dict:
    base = {
        "id": "808080",
        "conditionId": _CONDITION,
        "question": "Will it?",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.4", "0.6"]',
        "clobTokenIds": '["11", "22"]',
        "endDate": "2027-01-01T00:00:00Z",
        "active": True,
        "closed": False,
        "category": "Politics",
    }
    base.update(overrides)
    return base


def _adapter(market: dict) -> PolymarketAdapter:
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "clob" in str(request.url.host):
            return httpx.Response(200, json={"data": []})
        if path.endswith("/markets"):
            offset = request.url.params.get("offset", "0")
            return httpx.Response(200, json=[market] if offset == "0" else [])
        return httpx.Response(200, json=[])

    return PolymarketAdapter(transport=httpx.MockTransport(handle))


async def _fee(market: dict):
    markets = await _adapter(market).list_markets(status="open")
    assert markets, "fixture market did not parse"
    return markets[0].fee


@pytest.mark.asyncio
@pytest.mark.parametrize("rate", [0.03, 0.04, 0.05, 0.07])
async def test_the_published_rate_is_used_verbatim(rate: float) -> None:
    fee = await _fee(_market(
        feesEnabled=True,
        feeSchedule={"exponent": 1, "rate": rate, "takerOnly": True,
                     "rebateRate": 0.25},
    ))

    assert fee.taker_rate == pytest.approx(rate)
    assert fee.source == "venue_schedule"


@pytest.mark.asyncio
async def test_the_published_rate_beats_the_category_table() -> None:
    """The table guesses from a category string; the venue does not guess.

    Politics tables at 0.04 here, so a published 0.07 must win — this is
    the crypto case that was being understated live.
    """
    fee = await _fee(_market(
        category="Politics",
        feesEnabled=True,
        feeSchedule={"exponent": 1, "rate": 0.07, "takerOnly": True},
    ))

    assert fee.taker_rate == pytest.approx(0.07)


@pytest.mark.asyncio
async def test_fees_disabled_means_no_fee() -> None:
    """142 live markets say `feesEnabled: false`, and were being charged."""
    fee = await _fee(_market(feesEnabled=False, feeSchedule=None))

    assert fee.taker_rate == 0.0
    assert fee.maker_rate == 0.0
    assert fee.source == "venue_schedule"


@pytest.mark.asyncio
async def test_makers_still_pay_nothing() -> None:
    """`takerOnly: true` on all 1,776 published schedules."""
    fee = await _fee(_market(
        feesEnabled=True,
        feeSchedule={"exponent": 1, "rate": 0.04, "takerOnly": True,
                     "rebateRate": 0.25},
    ))

    assert fee.maker_rate == 0.0


@pytest.mark.asyncio
async def test_an_exponent_the_model_cannot_express_is_refused() -> None:
    """`PolymarketFeeModel` computes `rate * p * (1-p)` — exponent 1.

    A schedule with a different exponent describes a different curve, and
    applying its rate under our formula would be a confident wrong
    number. Fall back to the table and keep the table's provenance.
    """
    fee = await _fee(_market(
        category="Crypto",
        feesEnabled=True,
        feeSchedule={"exponent": 2, "rate": 0.04, "takerOnly": True},
    ))

    assert fee.source != "venue_schedule"
    assert fee.taker_rate == pytest.approx(0.07)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schedule",
    [None, {}, {"exponent": 1}, {"exponent": 1, "rate": "abc"},
     {"exponent": 1, "rate": -0.5}, {"exponent": 1, "rate": 1.5}, "not-an-object"],
)
async def test_an_unusable_schedule_falls_back_rather_than_guessing(schedule) -> None:
    fee = await _fee(_market(category="Crypto", feesEnabled=True,
                             feeSchedule=schedule))

    assert fee.taker_rate == pytest.approx(0.07)
    assert fee.source == "category_table"


# -- the maker rebate -------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("rebate", [0.15, 0.20, 0.25])
async def test_the_maker_rebate_is_captured_from_the_payload(rebate: float) -> None:
    """Polymarket PAYS makers where Kalshi charges them.

    A swing of ~0.0069 per contract at p=0.50, which is larger than the
    entire realised half-spread measured for passive quoting on Kalshi
    (+0.0051). Dropping this number on the floor would hide the fact
    that which venue to quote on outweighs how to quote.
    """
    fee = await _fee(_market(
        feesEnabled=True,
        feeSchedule={"exponent": 1, "rate": 0.04, "takerOnly": True,
                     "rebateRate": rebate},
    ))

    assert fee.maker_rebate_rate == pytest.approx(rebate)


@pytest.mark.asyncio
async def test_the_rebate_is_never_credited_as_a_negative_fee() -> None:
    """It is a daily programme payout, not a per-fill discount.

    Crediting it inside `fee()` would let projected revenue leak into
    every cost calculation in this repo as though it were banked — so
    `maker_rate` stays 0.0 and the model keeps returning a cost of zero,
    never a gain.
    """
    from app.venues.fees import PolymarketFeeModel

    fee = await _fee(_market(
        feesEnabled=True,
        feeSchedule={"exponent": 1, "rate": 0.07, "takerOnly": True,
                     "rebateRate": 0.25},
    ))

    assert fee.maker_rate == 0.0
    assert PolymarketFeeModel().fee(0.5, 100.0, "maker", fee) == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize("rebate", [None, "0.25", -0.1, 1.5, True])
async def test_an_unusable_rebate_is_zero_not_guessed(rebate) -> None:
    fee = await _fee(_market(
        feesEnabled=True,
        feeSchedule={"exponent": 1, "rate": 0.04, "rebateRate": rebate},
    ))

    assert fee.maker_rebate_rate == 0.0


def test_a_schedule_with_no_rebate_defaults_to_zero() -> None:
    """Every venue that pays nothing, which is the default case."""
    from app.venues.types import FeeSchedule

    assert FeeSchedule(taker_rate=0.07, maker_rate=0.0,
                       source="settings").maker_rebate_rate == 0.0
