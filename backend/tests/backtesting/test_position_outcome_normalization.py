"""Phase-1 remediation FIX 1: outcome-name casing must not fork a
position's identity.

The codebase is split on casing: Polymarket's Gamma payload spells outcomes
`"Yes"`/`"No"` verbatim while Kalshi's adapter and every strategy's own
hardcoded literal use `"YES"`/`"NO"`. Every COMPARISON against an outcome
name is case-insensitive, but every KEYING site (`Position.position_id`,
`Backtester._current_prices`) used to be case-sensitive — so a `"Yes"`-cased
position could never match the `"YES"`-keyed price the engine actually
tracks, and marked at its entry price FOREVER, silently. `Position.outcome`
and `Leg.outcome` now normalize at construction (`app.strategies.base.
normalize_outcome`), closing that off.

Every money number asserted here is computed BY HAND in a comment next to
the assertion (GUARDRAILS.md §5) — never with the code under test.
"""
import pytest

from app.services.backtesting import Portfolio, Position
from app.utils.time import utcnow

T0 = utcnow().replace(microsecond=0)


def test_position_id_normalizes_outcome_casing() -> None:
    """A `"Yes"`-cased `Position` must key identically to a `"YES"`-cased
    one — the whole point of normalizing at construction.
    """
    pos = Position(
        market_id="M",
        outcome="Yes",
        token_id="tok",
        entry_price=0.45,
        size=100.0,
        entry_time=T0,
        venue="polymarket",
    )
    assert pos.outcome == "YES"
    assert pos.position_id == "polymarket:M:YES"


def test_yes_cased_position_marks_at_market_not_entry() -> None:
    """The reviewer's repro: a `Position` constructed with a `"Yes"`-cased
    outcome must mark at MARKET (0.90), not freeze at its entry price
    (0.45), when priced against the engine's own `"YES"`/`"NO"`-keyed
    `_current_prices` dict.

    Before FIX 1: `position_id == "polymarket:M:Yes"` never matched the
    `"polymarket:M:YES"` price key, so `Portfolio.total_equity` fell back
    to `pos.entry_price` (0.45) — equity = 0.45 * 100 = 45.0.
    After FIX 1: `position_id == "polymarket:M:YES"` matches, so equity =
    0.90 * 100 = 90.0.
    """
    pos = Position(
        market_id="M",
        outcome="Yes",
        token_id="tok",
        entry_price=0.45,
        size=100.0,
        entry_time=T0,
        venue="polymarket",
    )
    portfolio = Portfolio(cash=0.0)
    portfolio.positions[pos.position_id] = pos

    prices = {"polymarket:M:YES": 0.90, "polymarket:M:NO": 0.10}

    assert portfolio.total_equity(prices) == pytest.approx(90.0)


@pytest.mark.parametrize("raw", ["Yes", "yes", "YES"])
def test_position_outcome_normalizes_every_yes_spelling(raw: str) -> None:
    pos = Position(
        market_id="M",
        outcome=raw,
        token_id="tok",
        entry_price=0.5,
        size=1.0,
        entry_time=T0,
    )
    assert pos.outcome == "YES"


@pytest.mark.parametrize("raw", ["No", "no", "NO"])
def test_position_outcome_normalizes_every_no_spelling(raw: str) -> None:
    pos = Position(
        market_id="M",
        outcome=raw,
        token_id="tok",
        entry_price=0.5,
        size=1.0,
        entry_time=T0,
    )
    assert pos.outcome == "NO"


def test_position_outcome_leaves_bundle_outcome_name_untouched() -> None:
    """A multi-outcome bundle's named outcome is not a YES/NO casing
    convention this normalization owns.
    """
    pos = Position(
        market_id="M",
        outcome="Trump",
        token_id="tok",
        entry_price=0.2,
        size=1.0,
        entry_time=T0,
    )
    assert pos.outcome == "Trump"
