"""`app.scripts.probe_quotable` — the T7 live health check, made to catch
a cap-induced blackout too (mm-proveout T7 retry).

Before this retry the probe's exit code keyed ONLY on `quotable == 0`, a
count taken BEFORE `settings.book_collection_top_n` caps `selected`. A
non-positive `book_collection_top_n` is now rejected at `Settings`
construction (`tests/services/test_book_collection_selection.py` pins
that), but this probe is the last line of defence against any OTHER way
`selected` could collapse to zero while `quotable` stays healthy -- a
`>= 1` cap that is simply too small, or a future regression upstream.
`selected == 0` while `quotable > 0` must fail loudly; `selected <
quotable` (the ordinary, intended shape of the cap) must stay quiet.

Every test here drives the real `probe()`/`main()` functions with an
INJECTED `FixtureAdapter` (GUARDRAILS.md §1.4: no network from a test),
never live data -- `get_market_data_adapters` is monkeypatched to return
fixture adapters built in-process.
"""
from typing import Any

import pytest

import app.scripts.probe_quotable as probe_quotable
import app.services.data_collector as dc
from app.venues.types import VenueId
from tests.venues.fixture_adapter import FixtureAdapter, make_venue_market


def _quotable_kalshi_market(market_id: str, volume: float) -> Any:
    """A Kalshi listing that clears the default spread/volume floors."""
    return make_venue_market(
        venue="kalshi",
        market_id=market_id,
        raw={"yes_bid_dollars": "0.40", "yes_ask_dollars": "0.55", "volume_24h_fp": volume},
    )


def _quotable_polymarket_market(market_id: str, volume: float) -> Any:
    """A Polymarket listing that clears the default spread/volume floors."""
    return make_venue_market(
        venue="polymarket",
        market_id=market_id,
        raw={"bestBid": 0.40, "bestAsk": 0.55, "volume24hr": volume},
    )


def _healthy_adapters(n_per_venue: int = 5) -> dict[VenueId, FixtureAdapter]:
    """Two adapters, each with `n_per_venue` two-sided, liquid markets --
    every one of them clears `book_collection_min_spread` (0.10, spread
    here is 0.15) and `book_collection_min_volume` (100.0, volume here
    starts at 500)."""
    kalshi = FixtureAdapter(venue="kalshi")
    polymarket = FixtureAdapter(venue="polymarket")
    for i in range(1, n_per_venue + 1):
        kalshi.add_market(_quotable_kalshi_market(f"k{i}", volume=500.0 + i))
        polymarket.add_market(_quotable_polymarket_market(f"p{i}", volume=500.0 + i))
    return {"kalshi": kalshi, "polymarket": polymarket}


def _patch_adapters(
    monkeypatch: pytest.MonkeyPatch, adapters: dict[VenueId, FixtureAdapter]
) -> None:
    async def fake_get_market_data_adapters() -> dict[VenueId, FixtureAdapter]:
        return adapters

    monkeypatch.setattr(
        probe_quotable, "get_market_data_adapters", fake_get_market_data_adapters
    )


# ---------------------------------------------------------------------------
# `_health_check_failures` — the pure classification the exit code uses.
# ---------------------------------------------------------------------------


def test_quotable_zero_is_a_failure() -> None:
    """Pre-existing check, must survive this retry unchanged."""
    failures = probe_quotable._health_check_failures(
        {"kalshi": (10, 5, 0, 0), "polymarket": (10, 8, 3, 3)}
    )

    assert len(failures) == 1
    assert "kalshi" in failures[0]
    assert "quotable=0" in failures[0]


def test_selected_zero_with_quotable_positive_is_a_failure() -> None:
    """The gap this retry closes: a healthy `quotable` count with a cap
    that ate every one of them must fail, not pass quietly."""
    failures = probe_quotable._health_check_failures(
        {"kalshi": (10, 8, 5, 0), "polymarket": (10, 8, 3, 3)}
    )

    assert len(failures) == 1
    assert "kalshi" in failures[0]
    assert "quotable=5" in failures[0] and "selected=0" in failures[0]


def test_selected_less_than_quotable_but_nonzero_is_not_a_failure() -> None:
    """The ordinary, intended shape of `book_collection_top_n` capping a
    larger quotable set -- must stay quiet."""
    failures = probe_quotable._health_check_failures(
        {"kalshi": (10, 8, 5, 2), "polymarket": (10, 8, 3, 3)}
    )

    assert failures == []


def test_quotable_zero_and_selected_zero_reports_once_not_twice() -> None:
    """`selected == 0` is implied by `quotable == 0`; the venue must not
    be reported by both checks at once."""
    failures = probe_quotable._health_check_failures({"kalshi": (10, 5, 0, 0)})

    assert len(failures) == 1


# ---------------------------------------------------------------------------
# `main()` end to end, over an injected adapter — never live data.
# ---------------------------------------------------------------------------


def test_main_exits_zero_when_both_venues_are_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity: the ordinary path (nothing capped away) must still pass."""
    _patch_adapters(monkeypatch, _healthy_adapters())

    assert probe_quotable.main() == 0


def test_main_exits_zero_when_top_n_caps_but_does_not_zero_a_venue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`selected < quotable` from a real, positive cap must stay quiet."""
    _patch_adapters(monkeypatch, _healthy_adapters(n_per_venue=5))
    monkeypatch.setattr(dc.settings, "book_collection_top_n", 2)

    assert probe_quotable.main() == 0


def test_main_exits_nonzero_when_top_n_zeroes_out_a_healthy_venue(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exact scenario the T7 retry brief reproduced: five quotable
    markets per venue, `book_collection_top_n=-10` -- Python's negative
    slice semantics turn that into `selected=[]` on BOTH venues while
    `quotable=5` on both, and the probe must now fail loudly instead of
    printing a healthy-looking table and exiting 0.

    `book_collection_top_n` is set directly on the shared settings
    singleton (as the rest of this test module and
    `tests/services/test_book_collection_selection.py` already do via
    `monkeypatch.setattr`) rather than through `Settings(...)`, because
    `Settings` now REJECTS this value at construction -- this test's
    job is to prove the probe is ALSO a backstop against `selected`
    collapsing to zero, independent of that construction-time guard.
    """
    _patch_adapters(monkeypatch, _healthy_adapters(n_per_venue=5))
    monkeypatch.setattr(dc.settings, "book_collection_top_n", -10)

    exit_code = probe_quotable.main()

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "kalshi" in err and "polymarket" in err
    assert "selected=0" in err


def test_main_still_exits_nonzero_on_the_original_quotable_zero_case(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep the pre-existing behaviour: a venue with nothing quotable at
    all (e.g. a renamed field) still fails, at the default `top_n=500`."""
    empty_kalshi = FixtureAdapter(venue="kalshi")
    healthy_polymarket = _healthy_adapters()["polymarket"]
    _patch_adapters(
        monkeypatch, {"kalshi": empty_kalshi, "polymarket": healthy_polymarket}
    )

    exit_code = probe_quotable.main()

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "kalshi" in err
    assert "quotable=0" in err
