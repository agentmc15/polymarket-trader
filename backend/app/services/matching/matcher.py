"""Deterministic cross-venue event matcher (T17, PLAN.md D9).

`score_pair` scores one candidate equivalence; `propose_links` scans two
venues' market lists and returns the pairs worth a human's attention.

WHAT THIS MODULE MAY AND MAY NOT DO. It PROPOSES. Every `EventLink` it
builds is born `"proposed"` and nothing here writes any other lifecycle
value — the transitions a person makes live entirely in
`app/api/routes/links.py`, and `tests/matching/test_matcher.py` plus the
task's own `grep` acceptance check keep it that way. That is structural,
not stylistic: PLAN.md R1 is that most apparent cross-venue edges are not
real, because two contracts that read alike can settle on different facts
(different resolution source, different timezone cutoff, different
handling of a tie or a postponement). A matcher that could promote its
own guess would turn a token-overlap score into a real-money position.
Token overlap cannot read a rules text; a reviewer can, which is why
`GET /links/{id}` puts the two rules texts side by side.

THE SCORE (PLAN.md D9)::

    confidence = 0.55 x title_jaccard
               + 0.20 x close_score
               + 0.15 x threshold_score
               + 0.10 x source_score

    close_score     = max(0, 1 - |close_delta_h| / 48)
    threshold_score = 1.0 agree / 0.5 unknown / 0.0 disagree
    source_score    = 1.0 agree / 0.5 unknown / 0.0 disagree

TRI-STATE, AND WHY UNKNOWN SCORES 0.5. `threshold_match` and
`source_match` are `bool | None`, not `bool`. `None` means "the
information is not there" — one venue simply does not publish a named
resolution source, or neither title states a number. Missing information
is NOT disagreement, and scoring it 0.0 would systematically suppress
legitimate pairs for the sin of one venue being terse. It scores 0.5:
halfway, contributing nothing either way.

TWO VETOES. Both cap the weighted sum from above, both sit below the
default `min_confidence`, and both only ever LOWER a score — they can
suppress a proposal, never promote one, which is the safe direction for
R1.

THE SECOND VETO: a close-time gap beyond `CLOSE_MISMATCH_HOURS` caps the
result at `CLOSE_MISMATCH_CAP`. `close_score` alone cannot do this job,
because it is a 0.20-weight CONTRIBUTION rather than a veto: two markets
with identical wording bank 0.55 from `title_jaccard` plus 0.125 from two
unknown tri-states, clearing the 0.5 floor with `close_score` at exactly
0.0. That is not hypothetical — "Will Benny Gantz be the next Prime
Minister of Israel?" exists on both venues with IDENTICAL titles and
close times eighteen years apart, and scored 0.68. A date is the other
dimension on which the same sentence names a different event, so it gets
the same treatment as a threshold.

THE FIRST VETO. A numeric-threshold DISAGREEMENT (`threshold_match is
False`: both titles state thresholds and they share no value) caps the
result at `THRESHOLD_MISMATCH_CAP`, below the default
`min_confidence`, so such a pair is never even proposed. "BTC above 100k"
and "BTC above 150k" are the same sentence and different events; without
the cap the weighted sum alone would score them ~0.80 on the strength of
identical wording, because the number itself normalizes to `<num>` in
both. The cap only ever LOWERS a score — it can suppress a proposal, it
can never promote one — which is the safe direction for R1.
"""
import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.models.event_link import EventLink
from app.services.matching.normalize import (
    NormalizedTitle,
    content_tokens,
    named_entities,
    normalize,
)
from app.strategies.base import normalize_outcome
from app.venues.types import VenueMarket

#: The D9 weights, kept in one place and echoed into every stored
#: `evidence` blob so a later kit can re-weight from the persisted
#: components without re-running the matcher over every market pair.
WEIGHTS: dict[str, float] = {
    "title": 0.55,
    "close": 0.20,
    "threshold": 0.15,
    "source": 0.10,
}

#: Close-time difference (hours) at which `close_score` reaches 0.
CLOSE_TOLERANCE_HOURS = 48.0

#: Score for a tri-state component whose inputs are missing. See the
#: module docstring: absent information is not disagreement.
UNKNOWN_SCORE = 0.5

#: Ceiling applied when two titles state thresholds that disagree. Below
#: `propose_links`' default `min_confidence` of 0.5 on purpose.
THRESHOLD_MISMATCH_CAP = 0.45

#: Ceiling applied when two markets close more than
#: `CLOSE_MISMATCH_HOURS` apart. Same value and same purpose as
#: `THRESHOLD_MISMATCH_CAP`: below `propose_links`' default
#: `min_confidence` of 0.5, so such a pair is never proposed.
CLOSE_MISMATCH_CAP = 0.45

#: Close-time gap beyond which the two markets are treated as naming
#: DIFFERENT events, whatever their wording. 30 days.
#:
#: Measured, not guessed: matching both venues' liquid subsets produced
#: 111 proposals whose close deltas were strictly bimodal — 103 closed
#: the same day and were genuine, 8 were 366+ days apart and were all
#: different events, and NOTHING fell in between. The constant sits far
#: below that observed gap on purpose. Two markets on one event can
#: legitimately close a little apart (timezone rollover, a venue
#: publishing the settlement deadline rather than the scheduled end,
#: Kalshi's early-close conditions) — a day or two, not a month.
CLOSE_MISMATCH_HOURS = 720.0

#: Relative tolerance for comparing two extracted thresholds.
_THRESHOLD_REL_TOL = 1e-9

#: The canonical binary outcome pair. `normalize_outcome` maps every
#: spelling either venue uses (Gamma's `"Yes"`/`"No"`, Kalshi's
#: `"YES"`/`"NO"`) onto these, and `outcome_map` must be stated in them —
#: a non-canonical key would build a `Leg` whose
#: `f"{venue}:{market_id}:{outcome}"` position id matches nothing (T18).
_BINARY = frozenset({"YES", "NO"})

#: The binary-to-binary outcome map. Not a module-level mutable default:
#: every `LinkEvidence` gets its own copy.
_BINARY_OUTCOME_MAP: dict[str, str] = {"YES": "YES", "NO": "NO"}


@dataclass(frozen=True)
class LinkEvidence:
    """Why the matcher scored one pair the way it did.

    Persisted whole into `event_links.evidence` (via `as_dict`). A bare
    confidence number is unreviewable — a reviewer needs to see that the
    titles agreed but the close times were three days apart, or that the
    score rode entirely on wording because neither venue named a
    resolution source.

    Attributes:
        title_jaccard: Jaccard similarity of the two normalized token
            SETS, in [0.0, 1.0]. `0.0` when both titles normalize to
            nothing.
        close_delta_h: ABSOLUTE hours between the two close times.
            Unsigned deliberately: the score uses `|Δh|`, and a reviewer
            reading `GET /links/{id}` sees both close times with their
            offsets anyway, so a signed value here would only invite a
            downstream `< 48` test that a negative delta would pass by
            accident.
        threshold_match: `True` if both titles state numeric thresholds
            and they agree; `False` if they state thresholds with no
            value in common; `None` if either side states none, or if
            they overlap only partially (ambiguous, not contradictory).
        source_match: `True`/`False` if both venues name a resolution
            source and it does or does not match after casefolding;
            `None` if either venue names none.
        outcome_map: Canonical outcome on A -> canonical outcome on B.
            `{"YES": "YES", "NO": "NO"}` for a binary-to-binary pair;
            `{}` for anything else, which a human must fill in at
            approval time.
        confidence: The final score in [0.0, 1.0], after the
            threshold-disagreement cap.
        thresholds_a: Thresholds extracted from A's title.
        thresholds_b: Thresholds extracted from B's title.
        shared_tokens: Tokens both titles have, sorted.
        distinct_tokens: Tokens exactly one title has, sorted — the ones
            worth a reviewer's eye.
        threshold_capped: `True` if the threshold veto fired, i.e. the
            reported `confidence` is lower than the weighted sum.
        close_capped: `True` if the close-time veto fired. Kept separate
            from `threshold_capped` so a reviewer reading
            `GET /links/{id}` sees WHICH disagreement suppressed the
            score, not merely that something did.
        entities_only_a: Named subjects A's question states and B's does
            not, sorted. See `entities_only_b`.
        entities_only_b: The same for B. Reported PER SIDE rather than
            merged, and deliberately NOT symmetric, because the two
            arrangements mean different things: subjects on BOTH sides
            means the questions are about different people ("Jon Ossoff"
            against "Jon Stewart"), while subjects on ONE side means
            either an added subject — a conjunction, and so a strictly
            narrower event that fakes a positive spread — or merely the
            other venue's shorter rendering of the same name. A reviewer
            can tell those apart; a scalar cannot, which is why this
            informs the score by exactly nothing.
    """

    title_jaccard: float
    close_delta_h: float
    threshold_match: bool | None
    source_match: bool | None
    outcome_map: dict[str, str]
    confidence: float
    thresholds_a: tuple[float, ...] = ()
    thresholds_b: tuple[float, ...] = ()
    shared_tokens: tuple[str, ...] = ()
    distinct_tokens: tuple[str, ...] = ()
    threshold_capped: bool = False
    close_capped: bool = False
    entities_only_a: tuple[str, ...] = ()
    entities_only_b: tuple[str, ...] = ()

    @property
    def needs_outcome_map(self) -> bool:
        """Whether a human must supply the outcome map before approval.

        Derived from `outcome_map` rather than stored separately so the
        two cannot drift apart.

        Returns:
            bool: `True` when the matcher could not derive a map (a
                multi-outcome pair).
        """
        return not self.outcome_map

    @property
    def close_score(self) -> float:
        """Close-time proximity component, in [0.0, 1.0]."""
        return max(0.0, 1.0 - self.close_delta_h / CLOSE_TOLERANCE_HOURS)

    @property
    def threshold_score(self) -> float:
        """Numeric-threshold component: 1.0 / 0.5 unknown / 0.0."""
        return tri_state_score(self.threshold_match)

    @property
    def source_score(self) -> float:
        """Resolution-source component: 1.0 / 0.5 unknown / 0.0."""
        return tri_state_score(self.source_match)

    def as_dict(self) -> dict[str, Any]:
        """Render the evidence as the JSON stored on `event_links`.

        Returns:
            dict[str, Any]: The components, the sub-scores they produce,
                the weights that combined them, and the final
                confidence — everything a reviewer or a later re-weighting
                pass needs, with no reference back to this object.
        """
        return {
            "title_jaccard": round(self.title_jaccard, 6),
            "close_delta_h": round(self.close_delta_h, 6),
            "threshold_match": self.threshold_match,
            "source_match": self.source_match,
            "close_score": round(self.close_score, 6),
            "threshold_score": self.threshold_score,
            "source_score": self.source_score,
            "confidence": round(self.confidence, 6),
            "weights": dict(WEIGHTS),
            "thresholds_a": list(self.thresholds_a),
            "thresholds_b": list(self.thresholds_b),
            "shared_tokens": list(self.shared_tokens),
            "distinct_tokens": list(self.distinct_tokens),
            "threshold_capped": self.threshold_capped,
            "close_capped": self.close_capped,
            "entities_only_a": list(self.entities_only_a),
            "entities_only_b": list(self.entities_only_b),
            "needs_outcome_map": self.needs_outcome_map,
        }


def tri_state_score(value: bool | None) -> float:
    """Score a tri-state agreement flag.

    Args:
        value: `True` (agree), `False` (disagree), or `None` (the
            information is absent on at least one side).

    Returns:
        float: `1.0`, `0.0`, or `UNKNOWN_SCORE` (0.5). `None` is NOT
            0.0 — see this module's docstring.
    """
    if value is None:
        return UNKNOWN_SCORE
    return 1.0 if value else 0.0


def jaccard(tokens_a: frozenset[str], tokens_b: frozenset[str]) -> float:
    """Jaccard similarity of two token sets.

    Args:
        tokens_a: First token set.
        tokens_b: Second token set.

    Returns:
        float: `|A n B| / |A u B|`, or `0.0` if both are empty — two
            titles that normalize to nothing are not evidence of
            anything.
    """
    union = tokens_a | tokens_b
    if not union:
        return 0.0
    return len(tokens_a & tokens_b) / len(union)


def compare_thresholds(
    thresholds_a: Sequence[float], thresholds_b: Sequence[float]
) -> bool | None:
    """Compare the numeric thresholds two titles state.

    Args:
        thresholds_a: Thresholds from A's title, in source order.
        thresholds_b: Thresholds from B's title, in source order.

    Returns:
        bool | None: `True` when the two multisets are equal within
            tolerance — the strikes are the same. `False` when both
            sides state thresholds and share NO value: that is a real
            contradiction ("above 100k" vs "above 150k") and triggers
            the module's one veto. `None` when either side states none
            (nothing to compare) OR when the sets overlap only partly:
            a partial overlap is one title mentioning an extra number,
            which is ambiguous, and turning ambiguity into a veto would
            suppress real pairs.
    """
    if not thresholds_a or not thresholds_b:
        return None
    sorted_a = sorted(thresholds_a)
    sorted_b = sorted(thresholds_b)
    if len(sorted_a) == len(sorted_b) and all(
        math.isclose(x, y, rel_tol=_THRESHOLD_REL_TOL)
        for x, y in zip(sorted_a, sorted_b, strict=True)
    ):
        return True
    if any(
        math.isclose(x, y, rel_tol=_THRESHOLD_REL_TOL)
        for x in sorted_a
        for y in sorted_b
    ):
        return None
    return False


def compare_sources(source_a: str | None, source_b: str | None) -> bool | None:
    """Compare two venues' named resolution sources.

    A string comparison is a weak instrument — "AP" and "the Associated
    Press call" are the same authority and compare `False` here — which
    is exactly why this contributes only 0.10 of the score and why the
    full strings go in front of a human on `GET /links/{id}`.

    Args:
        source_a: A's `resolution_source`, or `None`/blank if the venue
            names none.
        source_b: B's `resolution_source`, or `None`/blank.

    Returns:
        bool | None: `None` if either side names no source; otherwise
            whether they match after casefolding and whitespace
            collapse.
    """
    left = (source_a or "").strip()
    right = (source_b or "").strip()
    if not left or not right:
        return None
    return " ".join(left.casefold().split()) == " ".join(right.casefold().split())


def _is_binary(market: VenueMarket) -> bool:
    """Return whether a market is a canonical two-outcome YES/NO market."""
    return (
        len(market.outcomes) == 2
        and {normalize_outcome(name) for name in market.outcomes} == _BINARY
    )


def derive_outcome_map(a: VenueMarket, b: VenueMarket) -> dict[str, str]:
    """Derive the outcome correspondence between two markets.

    Args:
        a: Market on the first venue.
        b: Market on the second venue.

    Returns:
        dict[str, str]: `{"YES": "YES", "NO": "NO"}` when BOTH markets
            are canonical binaries. `{}` otherwise — a multi-outcome
            market's outcome names are venue-specific labels ("Trump",
            "Republican Party"), and guessing a correspondence between
            two such lists is exactly the kind of automatic string match
            PLAN.md R1 forbids feeding to the router. The empty map
            surfaces as `evidence["needs_outcome_map"] = True` and the
            reviewer supplies it at approval time.
    """
    if _is_binary(a) and _is_binary(b):
        return dict(_BINARY_OUTCOME_MAP)
    return {}


def score_pair(a: VenueMarket, b: VenueMarket) -> LinkEvidence:
    """Score one candidate cross-venue equivalence.

    Symmetric: `score_pair(a, b)` and `score_pair(b, a)` produce the same
    `confidence` and the same component values (`close_delta_h` is
    absolute, the token comparison is set-based, and both agreement tests
    are order-independent). Only `thresholds_a`/`thresholds_b` swap.

    Args:
        a: Market on the first venue. `question`/`rules_text` are
            untrusted venue text (GUARDRAILS.md §6) — scored, never
            interpreted.
        b: Market on the second venue.

    Returns:
        LinkEvidence: The components and the resulting confidence in
            [0.0, 1.0]. This function decides NOTHING about the pair's
            review lifecycle; see `propose_links`.
    """
    norm_a: NormalizedTitle = normalize(a.question)
    norm_b: NormalizedTitle = normalize(b.question)
    set_a = frozenset(norm_a.tokens)
    set_b = frozenset(norm_b.tokens)

    title_jaccard = jaccard(set_a, set_b)
    close_delta_h = abs((a.close_time - b.close_time).total_seconds()) / 3600.0
    # Deliberately from the RAW questions, not `norm_a`/`norm_b`:
    # capitalization is the entire signal and normalization destroys it.
    entities_a = named_entities(a.question)
    entities_b = named_entities(b.question)
    threshold_match = compare_thresholds(norm_a.thresholds, norm_b.thresholds)
    source_match = compare_sources(a.resolution_source, b.resolution_source)

    close_score = max(0.0, 1.0 - close_delta_h / CLOSE_TOLERANCE_HOURS)
    weighted = (
        WEIGHTS["title"] * title_jaccard
        + WEIGHTS["close"] * close_score
        + WEIGHTS["threshold"] * tri_state_score(threshold_match)
        + WEIGHTS["source"] * tri_state_score(source_match)
    )
    confidence = min(max(weighted, 0.0), 1.0)
    # Both vetoes are `min`-like: each lowers the score to its ceiling
    # only when the score is above it, so neither can ever promote a
    # pair, and applying both in sequence leaves the lower ceiling.
    capped = threshold_match is False and confidence > THRESHOLD_MISMATCH_CAP
    if capped:
        confidence = THRESHOLD_MISMATCH_CAP
    close_capped = (
        close_delta_h > CLOSE_MISMATCH_HOURS and confidence > CLOSE_MISMATCH_CAP
    )
    if close_capped:
        confidence = CLOSE_MISMATCH_CAP

    return LinkEvidence(
        title_jaccard=round(title_jaccard, 9),
        close_delta_h=round(close_delta_h, 9),
        threshold_match=threshold_match,
        source_match=source_match,
        outcome_map=derive_outcome_map(a, b),
        confidence=round(confidence, 9),
        thresholds_a=norm_a.thresholds,
        thresholds_b=norm_b.thresholds,
        shared_tokens=tuple(sorted(set_a & set_b)),
        distinct_tokens=tuple(sorted(set_a ^ set_b)),
        threshold_capped=capped,
        close_capped=close_capped,
        entities_only_a=tuple(sorted(entities_a - entities_b)),
        entities_only_b=tuple(sorted(entities_b - entities_a)),
    )


def candidate_pairs(
    markets_a: Sequence[VenueMarket], markets_b: Sequence[VenueMarket]
) -> list[tuple[int, int]]:
    """Block the cross product down to pairs that share a content token.

    A full scan is `O(len(a) x len(b))`, which is thousands-squared over
    two real venues. This builds an inverted index from content token to
    the B-markets carrying it, then unions the postings of each
    A-market's tokens.

    CORRECTNESS BEFORE SPEED. This must never drop a pair that shares a
    content token — a blocking step that silently discards a true pair is
    worse than a quadratic scan, because the miss is invisible: no
    exception, no counter, just a link that was never proposed and an
    equivalence a reviewer never got to see. So there is deliberately NO
    document-frequency cutoff here; a token carried by every market still
    contributes all of its postings, and the corpus that defeats this
    blocking (every market sharing one word) degrades to the quadratic
    scan rather than to a wrong answer.
    `tests/matching/test_matcher.py` pins this against a brute-force
    scan.

    The one thing it does NOT index on is `DATE_TOKEN`/`NUM_TOKEN`:
    nearly every market question carries a date, so those postings are
    the whole corpus and buy nothing. A pair whose ONLY overlap is
    `<date>` is therefore not a candidate — which is correct, since
    "closes on some date" is not evidence of anything.

    Args:
        markets_a: Markets from the first venue.
        markets_b: Markets from the second venue.

    Returns:
        list[tuple[int, int]]: `(index into markets_a, index into
            markets_b)` pairs, sorted, each appearing once.
    """
    index: dict[str, list[int]] = defaultdict(list)
    for j, market_b in enumerate(markets_b):
        for token in content_tokens(market_b.question):
            index[token].append(j)

    pairs: list[tuple[int, int]] = []
    for i, market_a in enumerate(markets_a):
        matched: set[int] = set()
        for token in content_tokens(market_a.question):
            matched.update(index.get(token, ()))
        pairs.extend((i, j) for j in sorted(matched))
    return pairs


def _ordered(
    a: VenueMarket, b: VenueMarket
) -> tuple[VenueMarket, VenueMarket]:
    """Order a pair canonically by `(venue, market_id)`.

    `event_links` is UNIQUE on `(venue_a, market_a, venue_b, market_b)`,
    so the same two markets must always land in the same column order
    however the scan reached them — otherwise a re-run with the venue
    lists swapped would insert a second row for the same equivalence and
    a reviewer would decide the same pair twice.
    """
    if (a.venue, a.market_id) <= (b.venue, b.market_id):
        return a, b
    return b, a


def propose_links(
    markets_a: Sequence[VenueMarket],
    markets_b: Sequence[VenueMarket],
    min_confidence: float = 0.5,
) -> list[EventLink]:
    """Propose cross-venue equivalences worth a human's review.

    Blocks with `candidate_pairs`, scores each survivor with
    `score_pair`, and returns an unsaved `EventLink` per pair scoring at
    or above `min_confidence` (PLAN.md D9: "anything >= 0.5 is written as
    `proposed`").

    EVERY row this returns is `"proposed"`, full stop. This function
    cannot express any other lifecycle value and must never learn to:
    the promotion of a proposal is a human act performed through
    `app/api/routes/links.py`, and that separation is the whole reason
    this subsystem exists (PLAN.md R1). The rows come back UNSAVED — the
    caller decides how they meet the database, and `POST /links/propose`
    in particular refuses to overwrite a pair a reviewer has already
    decided.

    Args:
        markets_a: Markets from the first venue.
        markets_b: Markets from the second venue.
        min_confidence: Inclusive score floor. Lower it to inspect what
            the matcher is rejecting; it does not change what may trade.

    Returns:
        list[EventLink]: Unsaved rows, highest confidence first, then by
            `(venue_a, market_a, venue_b, market_b)` for stable ordering.
            Each pair appears at most once, with its two markets in
            canonical column order.
    """
    # Keyed by the canonical four-tuple `event_links` is UNIQUE on, so a
    # venue that lists the same market twice cannot make this function
    # emit two rows that the database would then reject.
    best: dict[tuple[str, str, str, str], EventLink] = {}
    for i, j in candidate_pairs(markets_a, markets_b):
        market_a = markets_a[i]
        market_b = markets_b[j]
        # A market is not evidence for itself. Guards the case where the
        # same venue's list is passed twice (a same-venue complement is
        # `binary_complement_arbitrage`'s job, not a cross-venue link).
        if (
            market_a.venue == market_b.venue
            and market_a.market_id == market_b.market_id
        ):
            continue
        evidence = score_pair(market_a, market_b)
        if evidence.confidence < min_confidence:
            continue
        first, second = _ordered(market_a, market_b)
        key = (first.venue, first.market_id, second.venue, second.market_id)
        previous = best.get(key)
        if previous is not None and previous.confidence >= evidence.confidence:
            continue
        best[key] = EventLink(
            venue_a=first.venue,
            market_a=first.market_id,
            venue_b=second.venue,
            market_b=second.market_id,
            outcome_map=dict(evidence.outcome_map),
            confidence=evidence.confidence,
            evidence=evidence.as_dict(),
            status="proposed",
        )
    proposals = list(best.values())
    proposals.sort(
        key=lambda link: (
            -link.confidence,
            link.venue_a,
            link.market_a,
            link.venue_b,
            link.market_b,
        )
    )
    return proposals
