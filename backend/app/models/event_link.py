"""Persisted cross-venue event equivalence (T17, PLAN.md D9).

One `EventLink` row asserts that a market on venue A and a market on
venue B pay out on the SAME underlying fact. That assertion is the single
most dangerous claim this codebase makes: PLAN.md R1 spells out why.
Kalshi's "Will X happen by D?" and Polymarket's nearest market routinely
differ in resolution source ("official announcement" vs "credible
reporting"), timezone cutoff (ET midnight vs UTC), and edge-case handling
(ties, postponements, annulment). Two contracts that look identical to a
token-overlap score can settle DIFFERENTLY, and a cross-venue "arbitrage"
built on a wrong equivalence is not an arbitrage — it is two uncorrelated
directional bets that can both lose.

So the deterministic matcher (`app.services.matching.matcher`) is not the
product; a proposal a human reviewed is. That is why `status` starts at
`"proposed"` and why NOTHING in `app/services/matching/` may write any
other value: the only transitions to `"approved"`/`"rejected"` happen in
`app/api/routes/links.py`, driven by a person, and they are the only
writers of `reviewed_by`/`reviewed_at`. PLAN.md D9's rule — *nothing
trades on a `proposed` link* — is enforced downstream by
`app.strategies.cross_venue_arbitrage` (T18), which is handed only
reviewed links and raises if it is handed anything else.

MASS-ASSIGNMENT WARNING (see `app.models.intent` for the full writeup,
which applies verbatim here): SQLAlchemy's stock declarative constructor
sets ANY mapped column from a keyword, so `EventLink(**request.model_dump
())` would let an HTTP caller set `status`/`reviewed_by`/`reviewed_at`
directly and mint its OWN approval without a review ever happening. Every
route in `app/api/routes/links.py` therefore assigns these fields one by
one from a validated pydantic model, and `status` is never read off a
request body at all.

`evidence` deliberately stores the SCORE COMPONENTS, not just the number:
a bare confidence is unreviewable, and a later kit that wants to re-weight
the formula can do so from the stored components without re-running the
matcher over every market pair.
"""
from datetime import datetime
from typing import Literal

from sqlalchemy import (
    CheckConstraint,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, JSONDict, TimestampMixin

#: Review lifecycle of one proposed equivalence. `"proposed"` is the ONLY
#: value the matcher may write; the other two are human decisions made
#: through `app/api/routes/links.py`.
EventLinkStatus = Literal["proposed", "approved", "rejected"]

_STATUS_VALUES = ("proposed", "approved", "rejected")

#: Mirrors `app.venues.types.VenueId` exactly. Not imported — see
#: `app.models.trade._VENUE_VALUES` for why `app.models` does not import
#: from sibling packages at runtime; keep these in sync by hand.
_VENUE_VALUES = ("polymarket", "kalshi")


class EventLink(Base, TimestampMixin):
    """A proposed-or-reviewed claim that two markets resolve on one fact.

    The pair `(venue_a, market_a, venue_b, market_b)` is UNIQUE, and
    `app.services.matching.matcher.propose_links` orders each pair
    canonically by `(venue, market_id)` before building the row, so the
    same two markets always land on the same row no matter which
    adapter's list was scanned first. Re-running the proposer therefore
    UPDATES an existing unreviewed proposal rather than creating a
    duplicate — and never touches one a human has already decided.

    Attributes:
        id: Surrogate primary key, autoincrement. Deliberately NOT
            derived from anything the execution path mints: PLAN.md D4's
            `client_order_id`/`trade_id` are globally unique across paper
            AND live, so a link id that fed an order id could let a paper
            row collide with a live one. Link ids live in their own
            namespace and are never used to build an order id.
        venue_a: Venue of the first market (`"polymarket"`/`"kalshi"`).
        market_a: Venue-native market identifier on `venue_a`.
        venue_b: Venue of the second market.
        market_b: Venue-native market identifier on `venue_b`.
        outcome_map: Canonical outcome on A -> canonical outcome on B,
            e.g. `{"YES": "YES", "NO": "NO"}`. Keys and values are
            canonicalized through `app.strategies.base.normalize_outcome`
            (Polymarket's Gamma payload spells outcomes `"Yes"`/`"No"`
            while Kalshi's adapter forces `"YES"`/`"NO"`; a non-canonical
            key here would build a `Leg` whose
            `f"{venue}:{market_id}:{outcome}"` position id matches
            nothing). `{}` means the matcher could not derive one — a
            multi-outcome pair — and a human must supply it at approval
            time.
        confidence: Deterministic score in [0.0, 1.0] from
            `app.services.matching.matcher.score_pair`. Consumed by T18
            as `p_same_resolution`, the resolution-mismatch haircut on
            expected profit — so it is a probability, not a ranking key.
        evidence: The score's COMPONENTS (`title_jaccard`,
            `close_delta_h`, `threshold_match`, `source_match`, the
            per-component sub-scores, the shared/distinct tokens, and the
            weights used), so a reviewer can see WHY the pair was
            proposed. `needs_outcome_map` is `True` when `outcome_map` is
            empty.
        status: See `EventLinkStatus`. Indexed — the review queue is
            "everything still `proposed`" and the trading path is
            "everything `approved`", and both are status scans.
        reviewed_by: Who decided. `None` until a human does. Never
            written by the matcher.
        reviewed_at: Aware UTC time of that decision, `None` until then.
        notes: Free-text reviewer note — e.g. "Kalshi settles on the AP
            call, Polymarket on state certification; NOT the same fact".
            This is the field that carries what no token overlap can see.
        created_at: When the pair was first proposed (`TimestampMixin`).
        updated_at: Last write of any kind (`TimestampMixin`).
    """

    __tablename__ = "event_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    venue_a: Mapped[str] = mapped_column(String(16), nullable=False)
    market_a: Mapped[str] = mapped_column(String(128), nullable=False)
    venue_b: Mapped[str] = mapped_column(String(16), nullable=False)
    market_b: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome_map: Mapped[dict] = mapped_column(JSONDict, default=dict, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONDict, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        default="proposed",
        server_default="proposed",
        index=True,
        nullable=False,
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "venue_a",
            "market_a",
            "venue_b",
            "market_b",
            name="uq_event_links_pair",
        ),
        Index("ix_event_links_venue_a_market_a", "venue_a", "market_a"),
        Index("ix_event_links_venue_b_market_b", "venue_b", "market_b"),
        CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in _STATUS_VALUES)})",
            name="ck_event_links_status_valid",
        ),
        CheckConstraint(
            f"venue_a IN ({', '.join(repr(v) for v in _VENUE_VALUES)})",
            name="ck_event_links_venue_a_valid",
        ),
        CheckConstraint(
            f"venue_b IN ({', '.join(repr(v) for v in _VENUE_VALUES)})",
            name="ck_event_links_venue_b_valid",
        ),
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_event_links_confidence_range",
        ),
    )
