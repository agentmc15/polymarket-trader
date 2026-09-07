"""Surface the subjects one title names and the other does not.

WHY THIS EXISTS, measured on live data. Matching both venues' liquid
subsets produced 103 proposals, 75 of which showed a POSITIVE gross edge.
Reading them, 61 were false, and they failed in two shapes that are one
shape underneath:

  * SCOPE ASYMMETRY (54). Kalshi lists "Will Glenn Youngkin and Marco
    Rubio be the 2028 Republican Presidential ticket?"; the matcher pairs
    it with Polymarket's "Will Marco Rubio win the 2028 Republican
    presidential nomination?". The ticket is a CONJUNCTION — strictly
    contained in the nomination — so it is always cheaper, and therefore
    `narrow_ask + (1 - broad_bid) < 1` almost mechanically. That is not
    an edge; it is two bets that can both lose (Rubio takes the
    nomination with a different running mate and both legs pay nothing).
  * DIFFERENT SUBJECT (7). "Jon Ossoff" against "Jon Stewart", "Mark
    Cuban" against "Mark Kelly", "Barack Obama" against "Michelle Obama".
    A shared first name plus a shared template scores high on tokens.

Both are one thing: a NAMED SUBJECT present in exactly one question. A
prediction market's question is about a subject, so if one venue names a
subject the other does not, they are not the same question — either a
different one, or an extra one.

WHY THIS IS EVIDENCE AND NOT A VETO. `CLOSE_MISMATCH_CAP` earned its veto
because the live measurement separated perfectly: genuine pairs closed
the same day, false pairs were a year or more apart, nothing in between.
This signal does NOT separate that cleanly, and the audit found exactly
where it would misfire — venues abbreviate differently. Kalshi writes
"Benjamin Netanyahu" where Polymarket writes "Netanyahu"; one venue
writes "J.B. Pritzker", the other "JB Pritzker". Punctuation is handled
below, but a surname-only rendering of the SAME subject is a legitimate
pair that a veto would silently kill, and suppressing a real edge is the
failure this system can least afford to hide.

So it is reported, and the two sides are reported SEPARATELY, because
they carry different weight: entities on both sides means different
subjects (strong), entities on one side only means an added subject or a
name expansion (weaker, needs the reviewer's eye). That is the module's
own stated position — confidence alone is the wrong approval criterion,
the evidence fields are the signal.
"""
from datetime import timedelta

import pytest

from app.services.matching.matcher import WEIGHTS, score_pair
from app.services.matching.normalize import named_entities
from tests.matching.test_matcher import CLOSE, market


def test_named_entities_finds_the_subject_and_drops_the_scaffolding() -> None:
    got = named_entities("Will Marco Rubio win the 2028 Republican presidential nomination?")

    assert "rubio" in got and "marco" in got
    # Title-case scaffolding is not a subject.
    for scaffolding in ("will", "republican", "presidential"):
        assert scaffolding not in got


def test_punctuation_does_not_split_one_subject_into_two() -> None:
    """The audit's first misfire: "J.B. Pritzker" vs "JB Pritzker"."""
    assert named_entities("Will J.B. Pritzker run?") == named_entities(
        "Will JB Pritzker run?"
    )


def test_lowercase_words_are_never_subjects() -> None:
    """Extraction reads the RAW title, so case is the whole signal."""
    assert named_entities("will the fed cut rates in march?") == frozenset()


def test_a_conjunction_surfaces_the_extra_subject() -> None:
    """The 54-pair failure class, reproduced from the live measurement."""
    ticket = market(
        "kalshi", "K1",
        "Will Glenn Youngkin and Marco Rubio be the 2028 Republican Presidential ticket?",
    )
    nomination = market(
        "polymarket", "P1",
        "Will Marco Rubio win the 2028 Republican presidential nomination?",
    )
    evidence = score_pair(ticket, nomination).as_dict()

    assert set(evidence["entities_only_a"]) == {"glenn", "youngkin"}
    assert evidence["entities_only_b"] == []


def test_different_subjects_surface_on_both_sides() -> None:
    """The 7-pair failure class. Both sides non-empty is the strong signal."""
    a = market("kalshi", "K1", "Will Jon Ossoff be the Democratic Presidential nominee in 2028?")
    b = market("polymarket", "P1", "Will Jon Stewart win the 2028 Democratic presidential nomination?")
    evidence = score_pair(a, b).as_dict()

    assert evidence["entities_only_a"] == ["ossoff"]
    assert evidence["entities_only_b"] == ["stewart"]


def test_a_genuine_pair_surfaces_nothing() -> None:
    """One of the 14 that survived every filter, unchanged."""
    a = market("kalshi", "K1", "Will Andy Beshear be the Democratic Presidential nominee in 2028?")
    b = market("polymarket", "P1", "Will Andy Beshear win the 2028 Democratic presidential nomination?")
    evidence = score_pair(a, b).as_dict()

    assert evidence["entities_only_a"] == []
    assert evidence["entities_only_b"] == []


def test_the_asymmetry_does_not_move_the_score() -> None:
    """Deliberate, and pinned so it cannot drift into a veto by accident.

    Asserted structurally rather than by comparing two pairs: the score
    must remain exactly the four documented components under their
    documented weights, with no fifth term. If someone later decides the
    asymmetry SHOULD cap the score, that is a real decision with a real
    false-suppression cost, and it should break this test and be argued
    for — not arrive silently.
    """
    evidence = score_pair(
        market("kalshi", "K1",
               "Will Glenn Youngkin and Marco Rubio be the 2028 Republican ticket?"),
        market("polymarket", "P1",
               "Will Marco Rubio win the 2028 Republican nomination?"),
    )
    # The asymmetry is real and reported...
    assert evidence.entities_only_a == ("glenn", "youngkin")
    # ...and contributes nothing.
    assert not evidence.threshold_capped and not evidence.close_capped
    expected = (
        WEIGHTS["title"] * evidence.title_jaccard
        + WEIGHTS["close"] * evidence.close_score
        + WEIGHTS["threshold"] * evidence.threshold_score
        + WEIGHTS["source"] * evidence.source_score
    )
    assert evidence.confidence == pytest.approx(expected)


def test_the_asymmetry_is_reported_per_side_not_merged() -> None:
    """`score_pair(a, b)` and `score_pair(b, a)` swap the two sides.

    Unlike every other component this one is deliberately NOT symmetric:
    a reviewer needs to know WHICH venue named the extra subject.
    """
    a = market("kalshi", "K1", "Will Benjamin Netanyahu be arrested before Jan 1, 2027?")
    b = market("polymarket", "P1", "Netanyahu out by end of 2026?", close=CLOSE + timedelta(hours=1))

    forward = score_pair(a, b).as_dict()
    backward = score_pair(b, a).as_dict()

    assert forward["entities_only_a"] == backward["entities_only_b"]
    assert forward["entities_only_b"] == backward["entities_only_a"]
    # And the abbreviation case is visible rather than silently vetoed:
    # Kalshi's full name against Polymarket's surname.
    assert forward["entities_only_a"] == ["benjamin"]
