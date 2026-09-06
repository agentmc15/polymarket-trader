"""`BaseStrategy.declared_edge_basis` and the scan rejection it drives.

Nine strategies are registered. Four reach a live path: three arbitrage
ones via `scan()`, `settlement_edge` via `near_resolution_pass()`. The
route already refuses `?strategies=settlement_edge` with a 400, on the
stated principle that answering `200 {"found": 0}` reports "no edges"
when the truth is "this pass cannot see any".

`favorite_compounder` and `no_bias_exploit` have that identical
property, reached from the other side: both unconditionally declare a
DIRECTIONAL edge basis, `_published_edge` refuses every basis outside
`SCORABLE_EDGE_BASES` (T34), so `score()` raises `UnscorableIntent` for
100% of their intents and `scan()` skips every one. They answered
`200 {"found": 0}` until this change.

The tests below pin three things: the declaration matches what the
strategy actually stamps (so the class attribute cannot drift from the
metadata), the rejection set is DERIVED rather than hand-listed, and
the four scorable strategies are not swept up by it.
"""
import inspect

import pytest

from app.services.scanner import (
    ARBITRAGE_STRATEGIES,
    NEAR_RESOLUTION_STRATEGY,
    UNSCORABLE_BY_SCAN,
)
from app.services.scoring import SCORABLE_EDGE_BASES
from app.strategies import STRATEGIES, get_strategy
from app.strategies.base import EDGE_BASIS_DIRECTIONAL, EDGE_BASIS_KEY


def test_the_two_directional_strategies_are_the_unscorable_set() -> None:
    """Derived from `declared_edge_basis`, not written by hand."""
    assert UNSCORABLE_BY_SCAN == {
        "favorite_compounder": EDGE_BASIS_DIRECTIONAL,
        "no_bias_exploit": EDGE_BASIS_DIRECTIONAL,
    }


def test_no_scorable_strategy_is_rejected() -> None:
    """The four strategies on a live path must never land in the set.

    A false positive here is worse than the bug this closes: it would
    take a working discovery surface away.
    """
    for name in (*ARBITRAGE_STRATEGIES, NEAR_RESOLUTION_STRATEGY):
        assert name not in UNSCORABLE_BY_SCAN


def test_every_declared_basis_is_genuinely_unscorable() -> None:
    """A strategy only enters the set for a basis scoring actually refuses."""
    for name, basis in UNSCORABLE_BY_SCAN.items():
        assert basis not in SCORABLE_EDGE_BASES, name


@pytest.mark.parametrize("name", sorted(UNSCORABLE_BY_SCAN))
def test_declaration_matches_what_the_strategy_stamps(name: str) -> None:
    """The class attribute and the emitted metadata are ONE source.

    Both strategies stamp `EDGE_BASIS_KEY: self.declared_edge_basis`, so
    this holds structurally rather than by two literals agreeing. If
    someone re-hardcodes the metadata value, the class attribute stops
    predicting what `score()` sees and the route's 400 starts lying —
    this is what catches that.
    """
    strategy = get_strategy(name)
    source = type(strategy).__mro__[0]

    assert strategy.declared_edge_basis == UNSCORABLE_BY_SCAN[name]
    # The stamp reads the attribute rather than repeating its value.
    body = inspect.getsource(source)
    assert f"{EDGE_BASIS_KEY}: self.declared_edge_basis" in body or (
        "EDGE_BASIS_KEY: self.declared_edge_basis" in body
    ), f"{name} hardcodes its basis instead of stamping from the class attribute"


def test_strategies_without_a_declaration_are_untouched() -> None:
    """Absence means "ask the intent" — the pre-existing behaviour.

    `catalyst_momentum`, `correlation_hedging` and `term_structure_spreads`
    declare no basis and publish no `edge` key, so `_published_edge`
    returns 0.0 for them exactly as before. They are reachable via an
    explicit `?strategies=` and must stay reachable: this change refuses
    strategies that CANNOT be scored, not strategies that score poorly.
    """
    undeclared = {
        name
        for name, cls in STRATEGIES.items()
        if cls.declared_edge_basis is None
    }

    assert "catalyst_momentum" in undeclared
    assert "correlation_hedging" in undeclared
    assert "term_structure_spreads" in undeclared
    assert undeclared & set(UNSCORABLE_BY_SCAN) == set()
