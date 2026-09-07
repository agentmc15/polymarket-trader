"""A years-apart close is a different event, and must not be proposed.

MEASURED, not hypothesized. Matching the LIQUID subset of both venues
(Kalshi spread <=3c with >=100 contracts a side; Polymarket spread <=3c
with >=$1000 liquidity) produced 111 proposals, and the close-time delta
separated true from false matches perfectly:

    103 pairs closed the SAME DAY  -> every one a genuine equivalence
      0 pairs between 2d and 1y
      8 pairs >= 366 days apart    -> every one a different event

The three that survived every other filter and showed the LARGEST
apparent edges were all in that second group:

    +0.0300  "Will Republicans win the Senate race in Louisiana?"  (closes 2029)
             "Will the Republicans win the Louisiana Senate race in 2026?"
    +0.0210  "Will the US default on its debt by Dec 31, 2027?"    (closes 2028)
             "US defaults on debt by 2027?"
    +0.0030  "Will Benny Gantz be the next Prime Minister of Israel?" (closes 2045)
             "Will Benny Gantz be the next Prime Minister of Israel?"  (closes 2026)

That last pair is the argument in one line: IDENTICAL titles, eighteen
years apart, scoring 0.68 — comfortably past `propose_links`' 0.5 floor.
`close_score` cannot stop it, because it is a 0.20-weight CONTRIBUTION,
not a veto: a pair with identical wording banks 0.55 from
`title_jaccard` alone plus 0.125 from two unknown tri-states, so it
clears 0.5 with `close_score` at exactly 0.0.

This is verbatim the reasoning `THRESHOLD_MISMATCH_CAP` already exists
for — "BTC above 100k" and "BTC above 150k" are the same sentence and
different events — applied to the other dimension on which the same
sentence names a different event. So it gets the same mechanism, the
same ceiling, and the same one-directional guarantee: the cap only ever
LOWERS a score.

The horizon is 30 days, not the 366 the sample happens to show. Two
markets on one event can legitimately close a little apart (timezone
rollover, a venue publishing the settlement deadline rather than the
scheduled end, Kalshi's early-close conditions) — a day or two, not a
month. Fitting the constant to the observed gap would be fitting to one
afternoon's data.
"""
from datetime import UTC, datetime, timedelta

import pytest

from app.services.matching.matcher import (
    CLOSE_MISMATCH_CAP,
    CLOSE_MISMATCH_HOURS,
    propose_links,
    score_pair,
)
from tests.matching.test_matcher import market as _market

_BASE = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def _pair(delta: timedelta, question="Will Benny Gantz be the next Prime Minister of Israel?"):
    return (
        _market("kalshi", "K1", question, close=_BASE),
        _market("polymarket", "P1", question, close=_BASE + delta),
    )


def test_identical_titles_years_apart_are_not_proposed() -> None:
    """The Benny Gantz pair, reproduced from the live measurement."""
    a, b = _pair(timedelta(days=6576))

    assert score_pair(a, b).confidence <= CLOSE_MISMATCH_CAP
    assert propose_links([a], [b]) == []


def test_the_cap_is_recorded_as_evidence_a_reviewer_can_see() -> None:
    a, b = _pair(timedelta(days=1100))
    evidence = score_pair(a, b)

    assert evidence.close_capped is True
    assert evidence.as_dict()["close_capped"] is True
    # The raw delta survives the cap: a reviewer must be able to see WHY.
    assert evidence.as_dict()["close_delta_h"] == pytest.approx(1100 * 24)


def test_a_same_day_pair_is_untouched() -> None:
    """The 103 genuine pairs must keep scoring exactly as they did."""
    a, b = _pair(timedelta(hours=0))
    evidence = score_pair(a, b)

    assert evidence.close_capped is False
    assert evidence.confidence > CLOSE_MISMATCH_CAP
    assert propose_links([a], [b]) != []


def test_the_cap_only_lowers_never_promotes() -> None:
    """Same guarantee THRESHOLD_MISMATCH_CAP carries.

    A pair far apart in time AND unlike in wording already scores below
    the ceiling; the cap must not lift it up to the ceiling.
    """
    a = _market("kalshi", "K1", "Will the Chiefs win the Super Bowl?", close=_BASE)
    b = _market(
        "polymarket",
        "P1",
        "Federal Reserve cuts rates in March?",
        close=_BASE + timedelta(days=900),
    )
    evidence = score_pair(a, b)

    assert evidence.confidence < CLOSE_MISMATCH_CAP


def test_a_pair_just_inside_the_horizon_is_left_alone() -> None:
    """Boundary: the horizon is a threshold, not a decay."""
    inside = score_pair(*_pair(timedelta(hours=CLOSE_MISMATCH_HOURS - 1)))
    outside = score_pair(*_pair(timedelta(hours=CLOSE_MISMATCH_HOURS + 1)))

    assert inside.close_capped is False
    assert outside.close_capped is True


def test_the_cap_is_symmetric() -> None:
    """`score_pair(a, b)` and `score_pair(b, a)` must agree, as the
    docstring promises for every other component."""
    a, b = _pair(timedelta(days=400))

    assert score_pair(a, b).confidence == score_pair(b, a).confidence
    assert score_pair(a, b).close_capped == score_pair(b, a).close_capped
