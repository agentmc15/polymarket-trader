"""T39 L2/L3 — closing two admitted spellings in the edge-basis guard, and
turning malformed metadata into a per-intent skip instead of a whole-pass
crash (`app/services/scoring.py`, `_published_edge`, lines ~608-680).

Derived from a red-team pass over T34's edge-basis guard, not by reading
the fix and mirroring it back. Every scenario here uses a full `score()`
call against a hand-built `Intent`/`ScoreContext`, matching this
package's convention (`tests/services/test_scoring.py`) of testing the
public function rather than `_published_edge` directly.

No network, no live mode, no real order (GUARDRAILS.md §1.1/§1.2/§1.4):
every market/book is a hand-built fixture via
`tests.venues.fixture_adapter.make_venue_market` and
`tests.helpers.make_book`.

L2 — TWO SPELLINGS THAT USED TO BYPASS THE GUARD
-------------------------------------------------
`_published_edge` used to read the declared edge basis with
`intent.metadata.get(EDGE_BASIS_KEY)` and treat any `None` result —
whether the key was truly absent or was PRESENT with an explicit `None`
value — as "nothing declared", falling through to the omitted-key
default. Two ways to reach that same `None` without actually omitting
anything:

    1. `{EDGE_BASIS_KEY: None}` — an explicitly declared value outside
       the allowlist, let through by `is not None`.
    2. A key differing from `EDGE_BASIS_KEY` only by stray whitespace
       (e.g. `"edge_basis "`) — genuinely a different dict key, so
       `.get(EDGE_BASIS_KEY)` returns `None` and the real value is never
       read at all.

Neither is reachable today (every edge-publishing strategy imports the
constant and stamps it at one call site each), and closing them must NOT
touch the GENUINELY-omitted-key path: an intent that never mentions an
edge basis at all still scores as a plain fee-netted edge
(`scoring.py:285-298`), which is deliberate and covered by
`test_a_genuinely_omitted_edge_basis_still_scores_unchanged` below.

L3 — MALFORMED METADATA MUST SKIP ONE INTENT, NOT ABORT THE WHOLE PASS
-----------------------------------------------------------------------
`app.services.scanner.scan()` catches only `UnscorableIntent` around
`score()`; anything else propagates and kills the pass for every intent
behind the bad one. Two ways a buggy strategy could reach an unhandled
exception instead: an `EDGE_BASIS_KEY` value that is not hashable (e.g.
a `list`, from a strategy that meant to stamp one string and stamped a
one-element list instead) raised `TypeError` out of the `not in
frozenset(...)` check; `intent.metadata` that is not a mapping at all
raised `AttributeError` on `.get(...)`. Both now raise `UnscorableIntent`
instead.
"""
from datetime import timedelta
from typing import Any

import pytest

from app.config import settings
from app.services.scoring import ScoreContext, UnscorableIntent, score
from app.strategies.base import (
    EDGE_BASIS_DIRECTIONAL,
    EDGE_BASIS_IDENTITY_ESTIMATED,
    EDGE_BASIS_KEY,
    EDGE_BASIS_OBSERVED,
    IDENTITY_CONFIDENCE_KEY,
    IDENTITY_WORST_CASE_LOSS_KEY,
    Intent,
    Leg,
)
from app.utils.time import utcnow
from app.venues.types import VenueId, VenueMarket
from tests.helpers import make_book
from tests.venues.fixture_adapter import make_venue_market

PM: VenueId = "polymarket"
LONG_RULES_TEXT = "This market resolves according to the stated rules. " * 5  # 260 chars


def _open_market(market_id: str = "M1") -> VenueMarket:
    """A well-documented, far-from-close `VenueMarket` fixture.

    Long rules text and a named source so `resolution_risk` never enters
    the picture — every test here isolates the edge-basis guard, not the
    resolution-risk penalties.
    """
    return make_venue_market(
        PM,
        market_id,
        rules_text=LONG_RULES_TEXT,
        resolution_source="Official Source",
        close_time=utcnow() + timedelta(days=30),
    )


def _intent(metadata: Any, *, market_id: str = "M1") -> Intent:
    """A single-leg `Intent`, well-formed except for `metadata`.

    `metadata` is deliberately typed `Any`: several tests below
    construct one with a `metadata` that is not even a `dict`, which
    `Intent.__post_init__` never validates (it does not touch
    `metadata` at all) — the malformed value only matters once `score()`
    tries to read it.
    """
    return Intent(
        kind="single",
        legs=[
            Leg(
                market_id=market_id, outcome="YES", side="BUY", limit_price=0.50,
                size_contracts=10.0, venue=PM,
            ),
        ],
        hold_to_resolution=False,
        atomicity="best_effort",
        confidence=0.5,
        expected_resolution_ts=utcnow() + timedelta(hours=100),
        metadata=metadata,
    )


def _ctx(market_id: str = "M1") -> ScoreContext:
    """A `ScoreContext` with one open market and a matching book."""
    return ScoreContext(
        now=utcnow(),
        books={
            (PM, market_id, "YES"): make_book(
                bids=[(0.48, 100.0)], asks=[(0.50, 100.0)],
                venue=PM, market_id=market_id, outcome="YES",
            ),
        },
        markets={(PM, market_id): _open_market(market_id)},
        settings=settings,
    )


# ---------------------------------------------------------------------------
# L2 — the two closed spellings.
# ---------------------------------------------------------------------------


def test_an_explicit_none_edge_basis_is_refused_not_treated_as_omitted() -> None:
    """`{EDGE_BASIS_KEY: None}` is a DECLARED value, not an absent key.

    `None` is not one of `_SCORABLE_EDGE_BASES`, so this must refuse
    exactly as `"DIRECTIONAL_MISPRICING"` or `42` already did — reading
    it as "no basis declared" would score whatever `"edge"` holds as a
    plain fee-netted arbitrage edge with no basis for that trust at all.
    """
    intent = _intent({"edge": 5.0, EDGE_BASIS_KEY: None})

    with pytest.raises(UnscorableIntent, match=EDGE_BASIS_KEY):
        score(intent, _ctx())


def test_a_whitespace_mangled_edge_basis_key_is_refused_not_treated_as_omitted() -> None:
    """A key differing from `EDGE_BASIS_KEY` only by stray whitespace.

    `"edge_basis "` is a genuinely DIFFERENT dict key from `"edge_basis"`
    — `.get(EDGE_BASIS_KEY)` finds nothing under the real key and the
    declared `EDGE_BASIS_DIRECTIONAL` value is never read. Silently
    scoring this as "no basis declared" is the same failure as the
    `None` case above, reached from the strategy's side instead of a
    caller's.
    """
    mangled_key = EDGE_BASIS_KEY + " "
    intent = _intent({"edge": 5.0, mangled_key: EDGE_BASIS_DIRECTIONAL})

    with pytest.raises(UnscorableIntent, match="stray whitespace"):
        score(intent, _ctx())


def test_a_genuinely_omitted_edge_basis_still_scores_unchanged() -> None:
    """The regression half: closing the two spellings above must not touch
    the deliberate default for an intent that mentions NO edge basis at
    all (`scoring.py:285-298`) — every pre-T34 strategy relies on this.

    HAND-COMPUTED (GUARDRAILS.md §5): single market, no identity
    confidence declared, so `net_edge` is the published `0.02` untouched
    and `edge_basis` reads `EDGE_BASIS_OBSERVED`, exactly as it did
    before this task touched the guard at all.
    """
    intent = _intent({"edge": 0.02})

    result = score(intent, _ctx())

    assert result.net_edge == pytest.approx(0.02)
    assert result.edge_basis == EDGE_BASIS_OBSERVED


def test_a_key_merely_containing_edge_basis_as_a_substring_is_still_omitted() -> None:
    """Guard against an over-broad fix: `"an_edge_basis_note"` is not a
    near-miss of `EDGE_BASIS_KEY`, it is an unrelated key a strategy is
    free to carry for its own purposes, and must not be swept into the
    whitespace check above (which only matches `key.strip() ==
    EDGE_BASIS_KEY`, not substring containment).
    """
    intent = _intent({"edge": 0.02, "an_edge_basis_note": "unrelated"})

    result = score(intent, _ctx())

    assert result.net_edge == pytest.approx(0.02)
    assert result.edge_basis == EDGE_BASIS_OBSERVED


# ---------------------------------------------------------------------------
# L2 regression — the two real, still-legitimate bases score unchanged.
# ---------------------------------------------------------------------------


def test_an_observed_basis_strategy_still_scores_unchanged() -> None:
    """A strategy that explicitly stamps `EDGE_BASIS_OBSERVED` (rather
    than omitting the key) must be read identically to omitting it.
    """
    intent = _intent({"edge": 0.02, EDGE_BASIS_KEY: EDGE_BASIS_OBSERVED})

    result = score(intent, _ctx())

    assert result.net_edge == pytest.approx(0.02)
    assert result.edge_basis == EDGE_BASIS_OBSERVED


def test_an_identity_estimated_basis_strategy_still_haircuts_correctly() -> None:
    """`EDGE_BASIS_IDENTITY_ESTIMATED` with a real `p_same_resolution` and
    `worst_case_loss` still applies the identity haircut exactly as
    before (T31's formula).

    HAND-COMPUTED (GUARDRAILS.md §5): `edge * p - (1 - p) * worst_case`
    = `0.10 * 0.90 - 0.10 * 4.00` = `0.09 - 0.40` = `-0.31`.
    """
    intent = _intent(
        {
            "edge": 0.10,
            EDGE_BASIS_KEY: EDGE_BASIS_IDENTITY_ESTIMATED,
            IDENTITY_CONFIDENCE_KEY: 0.90,
            IDENTITY_WORST_CASE_LOSS_KEY: 4.00,
        }
    )

    result = score(intent, _ctx())

    assert result.net_edge == pytest.approx(0.09 - 0.40)
    assert result.net_edge == pytest.approx(-0.31)
    assert result.edge_basis == EDGE_BASIS_IDENTITY_ESTIMATED


def test_a_directional_basis_is_still_refused_exactly_as_before() -> None:
    """The guard's actual job (T34) must be untouched by the T39 fix."""
    intent = _intent({"edge": 0.02, EDGE_BASIS_KEY: EDGE_BASIS_DIRECTIONAL})

    with pytest.raises(UnscorableIntent, match=EDGE_BASIS_DIRECTIONAL):
        score(intent, _ctx())


# ---------------------------------------------------------------------------
# L3 — malformed metadata is a per-intent skip, not a pass-ending crash.
# ---------------------------------------------------------------------------


def test_an_unhashable_edge_basis_value_is_unscorable_not_a_crash() -> None:
    """A `list` under `EDGE_BASIS_KEY` used to raise `TypeError` (unhashable,
    from `not in frozenset(...)`), which `app.services.scanner.scan()`'s
    `except UnscorableIntent` around `score()` would NOT catch — the
    whole pass would die on this one intent. It must raise
    `UnscorableIntent` instead.
    """
    intent = _intent({"edge": 0.02, EDGE_BASIS_KEY: ["directional_mispricing"]})

    with pytest.raises(UnscorableIntent, match=EDGE_BASIS_KEY):
        score(intent, _ctx())


def test_non_mapping_metadata_is_unscorable_not_a_crash() -> None:
    """`intent.metadata` that is not a mapping at all used to raise
    `AttributeError` on `.get(...)` -- also invisible to `scan()`'s
    `except UnscorableIntent`. `Intent.__post_init__` never validates
    `metadata`'s type, so a strategy bug really can hand `score()` a
    `list` here.
    """
    intent = _intent(["not", "a", "mapping"])

    with pytest.raises(UnscorableIntent, match="mapping"):
        score(intent, _ctx())


# ---------------------------------------------------------------------------
# Red/green: the pre-T39 `_published_edge` logic, reproduced verbatim,
# against the exact four scenarios above.
# ---------------------------------------------------------------------------


def _pre_t39_published_edge(intent: Intent) -> float:
    """`_published_edge` as it read before T39 L2/L3.

    Reproduced literally (not derived from the current function, so a
    future refactor of the real one cannot accidentally make this
    "regain" the fix) purely so the test below can show it did not
    refuse the four scenarios this task closes. `_SCORABLE_EDGE_BASES`
    is inlined as the same two constants rather than imported, since it
    is a module-private name.
    """
    scorable = {EDGE_BASIS_OBSERVED, EDGE_BASIS_IDENTITY_ESTIMATED}
    declared_basis = intent.metadata.get(EDGE_BASIS_KEY)
    if declared_basis is not None and declared_basis not in scorable:
        raise UnscorableIntent(f"{EDGE_BASIS_KEY} not scorable: {declared_basis!r}")
    if "edge" in intent.metadata:
        return float(intent.metadata["edge"])
    if "net_edge" in intent.metadata:
        return float(intent.metadata["net_edge"])
    return 0.0


def test_the_pre_t39_logic_let_all_four_scenarios_through_the_fix_now_refuses() -> None:
    """RED: the old rule set's exact failure on each of the four inputs
    above. GREEN: `score()` (the fixed `_published_edge`) refuses every
    one of them with `UnscorableIntent`, proven by the four tests above
    already passing -- this test is the explicit side-by-side.
    """
    none_basis = _intent({"edge": 5.0, EDGE_BASIS_KEY: None})
    assert _pre_t39_published_edge(none_basis) == pytest.approx(5.0), (
        "RED: the old logic read the edge instead of refusing a declared None"
    )

    mangled_key = _intent({"edge": 5.0, EDGE_BASIS_KEY + " ": EDGE_BASIS_DIRECTIONAL})
    assert _pre_t39_published_edge(mangled_key) == pytest.approx(5.0), (
        "RED: the old logic never saw the mangled key and read the edge anyway"
    )

    unhashable = _intent({"edge": 0.02, EDGE_BASIS_KEY: ["directional_mispricing"]})
    with pytest.raises(TypeError):
        _pre_t39_published_edge(unhashable)  # RED: crashes the whole pass

    non_mapping = _intent(["not", "a", "mapping"])
    with pytest.raises(AttributeError):
        _pre_t39_published_edge(non_mapping)  # RED: crashes the whole pass

    # GREEN, all four, via the real public entry point:
    for bad_intent in (none_basis, mangled_key, unhashable, non_mapping):
        with pytest.raises(UnscorableIntent):
            score(bad_intent, _ctx())
