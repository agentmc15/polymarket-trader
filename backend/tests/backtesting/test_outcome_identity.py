"""T21d: an outcome's IDENTITY is one canonicalization, and an unmarkable
position is loud.

Phase-1 remediation FIX 1 closed a money bug for binary markets: two
spellings of the same outcome (`"Yes"` vs `"YES"`) produced two different
`Position.position_id`s, so a position never matched the price key the
engine actually tracked and marked at its entry price FOREVER, silently.
T21 then gave arbitrary, non-binary outcome labels first-class positions
and first-class depth — and the fix was never extended to them, because
`normalize_outcome()` canonicalizes `"YES"`/`"NO"` and is the identity
function for every other label. Everything that RESOLVED an outcome
case-folded it; everything that KEYED on one did not. The five defects
below are all that one asymmetry.

`app.strategies.base.outcome_key()` is the fix: ONE canonicalization,
used by every keying site (`Backtester._current_prices`,
`Position.position_id`, `Intent`'s bundle distinct-outcome check,
`BookSnapshot.outcome` at persist, `DataReplayer._get_recorded_book`'s
lookup), that strips whitespace and folds case for EVERY label.
`normalize_outcome()` stays the DISPLAY label and keeps the venue's own
capitalization of a candidate name.

Every money number asserted here is computed BY HAND in a comment next
to the assertion (GUARDRAILS.md §5) — never with the code under test.
"""
import logging
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.book_snapshot import BookSnapshot
from app.services.backtesting import (
    BacktestConfig,
    Backtester,
    InMemoryDataReplayer,
    Portfolio,
    Position,
)
from app.services.backtesting.data_replay import DataReplayer
from app.services.backtesting.engine import SlippageModel
from app.services.data_collector import DataCollector
from app.strategies.base import (
    BaseStrategy,
    Intent,
    Leg,
    MarketSnapshot,
    Signal,
    normalize_outcome,
    outcome_key,
)
from app.utils.time import utcnow
from app.venues.types import VenueId
from tests.helpers import make_book, make_snapshot
from tests.venues.fixture_adapter import FixtureAdapter, make_venue_market

T0 = utcnow().replace(microsecond=0)

ENGINE_LOGGER = "app.services.backtesting.engine"


class _NoTradeStrategy(BaseStrategy):
    """Never trades. The positions under test are injected directly."""

    name = "t21d_no_trade"

    def on_market_data(self, snapshot: MarketSnapshot) -> Intent | None:  # noqa: ARG002
        """Return `None` always."""
        return None

    def calculate_position_size(
        self,
        signal: Signal,  # noqa: ARG002
        portfolio_value: float,  # noqa: ARG002
        positions: dict[str, Any],  # noqa: ARG002
    ) -> float:
        """Unused: this strategy emits nothing to size."""
        return 0.0


def _config(**kw: Any) -> BacktestConfig:
    """Build a `BacktestConfig` with slippage padding OFF."""
    fields: dict[str, Any] = {
        "start_date": T0 - timedelta(minutes=1),
        "end_date": T0 + timedelta(days=1),
        "initial_capital": 10_000.0,
        "slippage_model": SlippageModel.NONE,
        "fill_at": "next",
    }
    fields.update(kw)
    return BacktestConfig(**fields)


def _backtester(**kw: Any) -> Backtester:
    """Build a `Backtester` over `_NoTradeStrategy`."""
    return Backtester(_config(**kw), _NoTradeStrategy())


def _bundle_position(outcome: str, **kw: Any) -> Position:
    """A 100-contract position entered at 0.20 on a non-binary outcome."""
    fields: dict[str, Any] = {
        "market_id": "M",
        "outcome": outcome,
        "token_id": "tok",
        "entry_price": 0.20,
        "size": 100.0,
        "entry_time": T0,
        "venue": "polymarket",
    }
    fields.update(kw)
    return Position(**fields)


# ---------------------------------------------------------------------------
# The canonicalization itself: display label vs identity.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Yes", "YES"),
        ("  yes  ", "YES"),
        ("NO", "NO"),
        ("Trump", "Trump"),
        ("TRUMP", "TRUMP"),
        ("Trump ", "Trump"),
        ("  Trump", "Trump"),
    ],
)
def test_normalize_outcome_is_the_display_label(raw: str, expected: str) -> None:
    """`normalize_outcome` canonicalizes the binary pair and strips
    whitespace, but never rewrites a venue's capitalization of a named
    outcome — that string goes on a screen and in a `TradeRecord`.
    """
    assert normalize_outcome(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Yes", "YES"),
        ("yes", "YES"),
        ("YES", "YES"),
        ("  no  ", "NO"),
        ("Trump", "trump"),
        ("TRUMP", "trump"),
        ("trump", "trump"),
        ("Trump ", "trump"),
        (" TRUMP ", "trump"),
    ],
)
def test_outcome_key_is_one_identity_for_every_label(raw: str, expected: str) -> None:
    """Every spelling of one outcome keys identically — the binary pair
    keeps its existing `"YES"`/`"NO"` identity so nothing written down
    before T21d changes.
    """
    assert outcome_key(raw) == expected


def test_bundle_intent_rejects_two_legs_that_are_one_outcome() -> None:
    """`"Trump"` and `"trump"` are the same outcome bought twice, not a
    two-outcome bundle — and left undetected the two legs would collapse
    onto ONE `position_id` while the bundle's arithmetic still assumed
    two independent legs.
    """
    legs = [
        Leg(market_id="M", outcome="Trump", side="BUY", limit_price=0.2),
        Leg(market_id="M", outcome="trump", side="BUY", limit_price=0.2),
        Leg(market_id="M", outcome="Biden", side="BUY", limit_price=0.2),
    ]
    with pytest.raises(ValueError, match="distinct outcomes"):
        Intent(
            kind="bundle",
            legs=legs,
            hold_to_resolution=True,
            atomicity="all_or_none",
            confidence=0.5,
        )


# ---------------------------------------------------------------------------
# Defect 5a: a non-binary position marked at its entry price forever.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_defect_5a_non_binary_position_marks_at_market_not_entry_price() -> None:
    """The red-team repro, exactly: a position spelled `"TRUMP"` priced by
    a payload spelled `"Trump"`.

    Before T21d the price-cache key was built from the PAYLOAD's spelling
    (`polymarket:M:Trump`) while `Position.position_id` was built from
    the POSITION's own (`polymarket:M:TRUMP`), so
    `Portfolio.total_equity`'s `prices.get(pos.position_id,
    pos.entry_price)` fell through to the entry price. Both now go
    through `position_key`/`outcome_key`.

    Money by hand — cash is the full initial capital because the position
    is injected rather than bought (no cash was ever spent on it), which
    is what makes the two equity numbers differ by exactly the mark:

        marked at MARKET (correct): 10_000.00 + 100 * 0.62 = 10_062.00
        marked at ENTRY  (the bug): 10_000.00 + 100 * 0.20 = 10_020.00
    """
    backtester = _backtester()
    pos = _bundle_position("TRUMP")
    backtester.portfolio.positions[pos.position_id] = pos

    snapshot = make_snapshot(
        market_id="M", ts=T0, yes=0.50, orderbook={"outcomes": {"Trump": {"ask": 0.62}}}
    )
    await backtester._process_snapshot(snapshot)

    assert pos.position_id == "polymarket:M:trump"
    assert backtester._current_prices["polymarket:M:trump"] == pytest.approx(0.62)

    equity = backtester.portfolio.total_equity(backtester._current_prices)
    assert equity == pytest.approx(10_062.00)  # 10_000 + 100 * 0.62
    assert equity != pytest.approx(10_020.00)  # 10_000 + 100 * 0.20, the bug's number

    # Nothing was left unmarked, so nothing is flagged.
    assert backtester._unmarked_positions == {}


def test_defect_5a_every_spelling_of_one_outcome_is_one_position() -> None:
    """Four spellings of one outcome must be ONE position id, or the
    engine holds four positions in a market that has one.
    """
    ids = {
        _bundle_position(spelling).position_id
        for spelling in ("Trump", "TRUMP", "trump", "Trump ")
    }
    assert ids == {"polymarket:M:trump"}


# ---------------------------------------------------------------------------
# Defect 5b: a trailing space also disabled stop-loss and take-profit.
# ---------------------------------------------------------------------------


def test_defect_5b_trailing_space_still_resolves_a_price() -> None:
    """Whitespace defeated BOTH the cache key and the resolver, because
    `normalize_outcome` stripped only inside its binary lookup.
    """
    backtester = _backtester()
    snapshot = make_snapshot(
        market_id="M", ts=T0, yes=0.50, orderbook={"outcomes": {"Trump ": {"ask": 0.62}}}
    )

    assert backtester._price_for_outcome(snapshot, "Trump ") == pytest.approx(0.62)
    # This one returned `None` before T21d.
    assert backtester._price_for_outcome(snapshot, "Trump") == pytest.approx(0.62)
    assert backtester._price_for_outcome(snapshot, "TRUMP") == pytest.approx(0.62)


@pytest.mark.asyncio
async def test_defect_5b_trailing_space_payload_marks_at_market_not_entry() -> None:
    """The money consequence of the whitespace hole, end to end.

    A position `"Trump"` in a market whose payload spells the outcome
    `"Trump "` used to be unpriceable AND unkeyable, so it marked at its
    entry price for the whole run.

        marked at MARKET (correct): 10_000.00 + 100 * 0.62 = 10_062.00
        marked at ENTRY  (the bug): 10_000.00 + 100 * 0.20 = 10_020.00
    """
    backtester = _backtester()
    pos = _bundle_position("Trump")
    backtester.portfolio.positions[pos.position_id] = pos

    replayer = InMemoryDataReplayer(
        [
            make_snapshot(
                market_id="M",
                ts=T0,
                yes=0.50,
                category="politics",
                orderbook={"outcomes": {"Trump ": {"ask": 0.62}}},
            )
        ]
    )
    result = await backtester.run(replayer)

    assert result.final_value == pytest.approx(10_062.00)  # 10_000 + 100 * 0.62
    assert result.final_value != pytest.approx(10_020.00)  # the bug's number
    assert result.unmarked_positions == ()


@pytest.mark.asyncio
async def test_defect_5b_trailing_space_payload_still_evaluates_the_stop_loss(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The stop-loss must be EVALUATED for a position the payload spells
    with a trailing space.

    Before T21d `_price_for_outcome` returned `None` here and
    `_check_position_exits` `continue`d past the position on every single
    tick — the stop was never evaluated once, for the whole run. It is
    now evaluated and HIT (`0.62 <= 0.90`); the position is not actually
    closed only because this snapshot carries no book to sell into, and
    the engine will not fabricate a fill for an exit. That distinction is
    exactly what the two log lines below separate: "evaluated, no
    liquidity" is a market condition, "never evaluated" was a defect.
    """
    backtester = _backtester()
    pos = _bundle_position("Trump", stop_loss=0.90)
    backtester.portfolio.positions[pos.position_id] = pos

    snapshot = make_snapshot(
        market_id="M",
        ts=T0,
        yes=0.50,
        category="politics",
        orderbook={"outcomes": {"Trump ": {"ask": 0.62}}},
    )
    with caplog.at_level(logging.DEBUG, logger=ENGINE_LOGGER):
        await backtester._process_snapshot(snapshot)

    messages = [record.getMessage() for record in caplog.records]
    assert any("exit skipped: no bid to sell into" in m for m in messages)
    assert not any("stop-loss/take-profit NOT evaluated" in m for m in messages)


@pytest.mark.asyncio
async def test_defect_5b_differently_cased_book_exits_the_position() -> None:
    """The same identity fix, through the exit path with real depth: a
    position `"Trump"` and a recorded book `"TRUMP"` are one outcome.

    Money by hand. The position is 100 contracts entered at 0.20
    (`cost_basis = 100 * 0.20 = 20.00`) with `stop_loss=0.40`. The book
    marks it at its mid — `(0.35 + 0.37) / 2 = 0.36`, at or below the
    stop — so the exit fires and walks the one bid level:

        exit price   = 0.35
        proceeds     = 100 * 0.35                       = 35.00
        fee          = 100 * 0.04 * 0.35 * 0.65         = 0.91
        realized P&L = 35.00 - 0.91 - 20.00             = 14.09
    """
    book = make_book(
        bids=[(0.35, 100.0)],
        asks=[(0.37, 100.0)],
        market_id="M",
        outcome="TRUMP",
        ts=T0,
    )
    backtester = _backtester()
    pos = _bundle_position("Trump", stop_loss=0.40)
    backtester.portfolio.positions[pos.position_id] = pos

    snapshot = make_snapshot(
        market_id="M", ts=T0, yes=0.50, category="politics", book=book
    )
    await backtester._process_snapshot(snapshot)

    assert pos.position_id not in backtester.portfolio.positions  # the stop fired
    sells = [t for t in backtester.trades if t.side == "SELL"]
    assert len(sells) == 1
    assert sells[0].price == pytest.approx(0.35)
    assert sells[0].fee == pytest.approx(0.91)  # 100 * 0.04 * 0.35 * 0.65
    assert sells[0].pnl == pytest.approx(14.09)  # 35.00 - 0.91 - 20.00


# ---------------------------------------------------------------------------
# Defect 5c: an unmarkable position was completely silent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_defect_5c_unmarkable_position_is_reported_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A position no snapshot can price is marked at its ENTRY PRICE by
    `Portfolio.total_equity`, which is indistinguishable from a position
    that has not moved. That must be LOUD.

    Before T21d there was no counter, no log and no `errors` entry
    anywhere for this — `grep -rn "unmarked\\|unpriced\\|mark_miss" app/`
    returned nothing — which is why defects 5a and 5b went unnoticed.

    The market here quotes only YES/NO, so the `"Trump"` position can be
    priced from nothing at all. Money by hand:

        final_value = 10_000.00 + 100 * 0.20 (the ENTRY price) = 10_020.00

    and `unmarked_positions` is what says that 10_020.00 is not a market
    number.
    """
    backtester = _backtester()
    pos = _bundle_position("Trump")
    backtester.portfolio.positions[pos.position_id] = pos

    replayer = InMemoryDataReplayer(
        [
            make_snapshot(market_id="M", ts=T0, yes=0.50, category="politics"),
            make_snapshot(
                market_id="M",
                ts=T0 + timedelta(minutes=5),
                yes=0.55,
                category="politics",
            ),
        ]
    )

    with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
        result = await backtester.run(replayer)

    assert result.unmarked_positions == ("polymarket:M:trump",)
    assert result.final_value == pytest.approx(10_020.00)  # 10_000 + 100 * 0.20

    warnings = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and getattr(record, "position_id", None) == "polymarket:M:trump"
    ]
    # Logged, and logged ONCE rather than on every tick of the replay.
    assert len(warnings) == 1
    assert "ENTRY PRICE" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_defect_5c_a_markable_position_is_not_flagged() -> None:
    """The counter must stay empty for a run whose positions all mark —
    a warning nobody can trust is worth nothing.
    """
    backtester = _backtester()
    pos = _bundle_position("TRUMP")
    backtester.portfolio.positions[pos.position_id] = pos

    replayer = InMemoryDataReplayer(
        [
            make_snapshot(
                market_id="M",
                ts=T0,
                yes=0.50,
                category="politics",
                orderbook={"outcomes": {"Trump": 0.62}},
            )
        ]
    )
    result = await backtester.run(replayer)

    assert result.unmarked_positions == ()
    assert result.final_value == pytest.approx(10_062.00)  # 10_000 + 100 * 0.62


@pytest.mark.asyncio
async def test_defect_5c_skipped_exit_check_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A protective order that is not being evaluated is worse than one
    that does not exist, because the result still reports it as set.
    """
    backtester = _backtester()
    pos = _bundle_position("Trump", stop_loss=0.90)
    backtester.portfolio.positions[pos.position_id] = pos

    snapshot = make_snapshot(market_id="M", ts=T0, yes=0.50, category="politics")
    with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
        await backtester._check_position_exits(snapshot)

    messages = [record.getMessage() for record in caplog.records]
    assert any("stop-loss/take-profit NOT evaluated" in message for message in messages)


# ---------------------------------------------------------------------------
# Defect 6: `BookSnapshot` identity was not casing-safe for non-binary labels.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_defect_6_collect_books_dedupes_every_spelling_of_one_outcome(
    test_session: AsyncSession,
) -> None:
    """`BookSnapshot`'s docstring promises a lookup by
    `(venue, market_id, outcome)` "can never miss a row purely because of
    a casing mismatch between two venues' payloads". Before T21d that
    held for exactly the binary case that did not need it.

    Four `collect_books` runs of the SAME book at the SAME `ts`, spelled
    `Trump`/`TRUMP`/`trump`/`"Trump "`, wrote FOUR rows straight past the
    unique constraint that exists to make that impossible. The binary
    control (`Yes`/`YES`/`yes`/`yEs`) was correct all along and must stay
    correct.
    """
    spellings = ("Trump", "TRUMP", "trump", "Trump ")
    binary_spellings = ("Yes", "YES", "yes", "yEs")
    collector = DataCollector(test_session)
    market_ids_per_venue: dict[VenueId, list[str]] = {"polymarket": ["PM-1"]}
    written = 0

    for named, binary in zip(spellings, binary_spellings, strict=True):
        market = make_venue_market(
            venue="polymarket",
            market_id="PM-1",
            outcomes=(named, binary),
            raw={"volume": 50_000.0},
        )
        adapter = (
            FixtureAdapter("polymarket")
            .add_market(market)
            .set_book(
                make_book(
                    bids=[(0.19, 100.0)],
                    asks=[(0.21, 100.0)],
                    market_id="PM-1",
                    outcome=named,
                    ts=T0,
                )
            )
            .set_book(
                make_book(
                    bids=[(0.44, 100.0)],
                    asks=[(0.46, 100.0)],
                    market_id="PM-1",
                    outcome=binary,
                    ts=T0,
                )
            )
        )
        written += await collector.collect_books(
            {"polymarket": adapter}, market_ids_per_venue, test_session
        )

    # One row for the named outcome and one for the binary one, written
    # by the FIRST run; the other three runs are pure no-ops.
    assert written == 2

    rows = (await test_session.execute(select(BookSnapshot))).scalars().all()
    assert sorted(row.outcome for row in rows) == ["YES", "trump"]


@pytest.mark.asyncio
async def test_defect_6_replayer_finds_the_row_under_any_spelling(
    test_session: AsyncSession,
) -> None:
    """`_get_recorded_book` matches `outcome` exactly, so a replay asking
    for `"Trump"` used to miss a row stored as `"TRUMP"` and fall through
    to `_book_for`'s non-binary skip — no depth at all, and a bundle leg
    that could never fill. Both sides now canonicalize the same way.
    """
    test_session.add(
        BookSnapshot(
            venue="polymarket",
            market_id="PM-1",
            outcome=outcome_key("TRUMP"),
            ts=T0,
            bids=[{"price": 0.19, "size": 100.0}],
            asks=[{"price": 0.21, "size": 100.0}],
            tick_size=0.01,
            min_size=1.0,
            depth_source="recorded",
        )
    )
    await test_session.flush()

    replayer = DataReplayer(
        session=test_session,
        start_date=T0 - timedelta(days=1),
        end_date=T0 + timedelta(days=1),
    )
    for spelling in ("TRUMP", "Trump", "trump", "Trump "):
        book = await replayer._get_recorded_book(
            venue="polymarket", market_id="PM-1", outcome=spelling, ts=T0
        )
        assert book is not None, spelling
        best_ask = book.best_ask()
        assert best_ask is not None
        assert best_ask.price == pytest.approx(0.21)


# ---------------------------------------------------------------------------
# Defect 8: `{"ask": null}` marked at entry price.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"ask": 0.62}, 0.62),
        ({"price": 0.62}, 0.62),
        ({"ask": None, "price": 0.62}, 0.62),
        ({"bid": 0.60, "ask": None}, 0.60),
        ({"ask": None, "price": None, "bid": 0.60}, 0.60),
        (0.62, 0.62),
        ("0.62", 0.62),
    ],
)
def test_defect_8_null_ask_falls_through_to_the_next_quote(
    payload: Any, expected: float
) -> None:
    """`value.get("ask", value.get("price"))` returns `None` when `"ask"`
    is PRESENT AND NULL — `dict.get`'s default is consulted only for a
    MISSING key. A one-sided book serialized with an explicit null ask is
    an ordinary venue payload, and it made the engine fall back to the
    position's entry price with a perfectly good bid in hand.
    """
    backtester = _backtester()
    snapshot = make_snapshot(
        market_id="M", ts=T0, yes=0.50, orderbook={"outcomes": {"Trump": payload}}
    )
    assert backtester._price_for_outcome(snapshot, "Trump") == pytest.approx(expected)


@pytest.mark.parametrize("payload", [{}, {"ask": None}, {"ask": "n/a"}, None])
def test_defect_8_a_payload_with_no_usable_quote_is_still_unpriceable(
    payload: Any,
) -> None:
    """The fix must not invent a price where the payload carries none —
    `None` stays `None`, and the position is reported unmarked instead.
    """
    backtester = _backtester()
    snapshot = make_snapshot(
        market_id="M", ts=T0, yes=0.50, orderbook={"outcomes": {"Trump": payload}}
    )
    assert backtester._price_for_outcome(snapshot, "Trump") is None


# ---------------------------------------------------------------------------
# Defect 9: two labels differing only in case both marked at the first one's
# price.
# ---------------------------------------------------------------------------


def test_defect_9_colliding_labels_are_detected_not_silently_picked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`{"Trump": {"ask": 0.62}, "trump": {"ask": 0.11}}` used to answer
    `0.62` for BOTH labels — the first casefold match in dict order. Dict
    order is not a pricing rule, and picking one of two contradictory
    prices for a real position is the failure this whole task removes.
    """
    backtester = _backtester()
    snapshot = make_snapshot(
        market_id="M",
        ts=T0,
        yes=0.50,
        orderbook={"outcomes": {"Trump": {"ask": 0.62}, "trump": {"ask": 0.11}}},
    )

    with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
        assert backtester._price_for_outcome(snapshot, "trump") is None
        assert backtester._price_for_outcome(snapshot, "Trump") is None

    assert any(
        "ambiguous outcome payload" in record.getMessage() for record in caplog.records
    )


def test_defect_9_colliding_labels_that_agree_are_not_ambiguous() -> None:
    """Two spellings quoting the SAME price are a redundant payload, not a
    contradictory one — there is nothing to pick between, so it prices.
    """
    backtester = _backtester()
    snapshot = make_snapshot(
        market_id="M",
        ts=T0,
        yes=0.50,
        orderbook={"outcomes": {"Trump": {"ask": 0.62}, "trump": {"ask": 0.62}}},
    )
    assert backtester._price_for_outcome(snapshot, "TRUMP") == pytest.approx(0.62)


@pytest.mark.asyncio
async def test_defect_9_an_ambiguous_outcome_leaves_the_position_flagged() -> None:
    """An outcome the engine refuses to price is exactly the case defect
    5c makes loud: the position marks at entry and the result says so.
    """
    backtester = _backtester()
    pos = _bundle_position("Trump")
    backtester.portfolio.positions[pos.position_id] = pos

    replayer = InMemoryDataReplayer(
        [
            make_snapshot(
                market_id="M",
                ts=T0,
                yes=0.50,
                category="politics",
                orderbook={
                    "outcomes": {"Trump": {"ask": 0.62}, "trump": {"ask": 0.11}}
                },
            )
        ]
    )
    result = await backtester.run(replayer)

    assert result.unmarked_positions == ("polymarket:M:trump",)
    assert result.final_value == pytest.approx(10_020.00)  # 10_000 + 100 * 0.20


# ---------------------------------------------------------------------------
# The binary path is untouched: every identity written down before T21d is
# byte-identical.
# ---------------------------------------------------------------------------


def test_binary_position_ids_are_unchanged() -> None:
    """`"YES"`/`"NO"` keep their canonical identity rather than folding to
    `"yes"`/`"no"`, so no position id, price key or `BookSnapshot` row
    written before T21d changes meaning.
    """
    portfolio = Portfolio(cash=0.0)
    for raw in ("Yes", "yes", "YES"):
        pos = Position(
            market_id="M",
            outcome=raw,
            token_id="tok",
            entry_price=0.45,
            size=100.0,
            entry_time=T0,
            venue="polymarket",
        )
        assert pos.position_id == "polymarket:M:YES"
        portfolio.positions[pos.position_id] = pos

    # 0.90 * 100 = 90.0 — the same number Phase-1 FIX 1's own test asserts.
    assert portfolio.total_equity({"polymarket:M:YES": 0.90}) == pytest.approx(90.0)
