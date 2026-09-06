"""Cross-venue event-link review API (T17, PLAN.md D9).

This module is the human half of the event-equivalence subsystem, and
the ONLY place in the codebase where an `event_links` row leaves the
`"proposed"` lifecycle. `app/services/matching/` scores pairs and writes
proposals; a person reads them here and decides. That split is
structural, not a convention: PLAN.md R1 records that Kalshi's and
Polymarket's nearest-equivalent contracts routinely differ in resolution
source, settlement wording, timezone cutoff and edge-case handling, so
two markets that a token-overlap score calls identical can settle
DIFFERENTLY — at which point the "arbitrage" built on them is two
uncorrelated directional bets that can both lose.

`GET /links/{id}` is therefore doing real work, not formatting. It puts
the two `rules_text` values, the two `close_time`s (with their offsets
visible) and the two `resolution_source`s in the SAME fields in the SAME
order, plus a field-by-field `comparison` marking what differs, so a
reviewer can catch "resolves per the AP call" against "resolves per state
certification" — a difference no amount of token overlap can see.

MASS ASSIGNMENT (Phase 0 security audit; `app.models.event_link` carries
the writeup). Nothing here builds or updates a row via
`EventLink(**request.model_dump())`. `status`/`reviewed_by`/`reviewed_at`
are assigned one by one from a validated pydantic model, and `status` is
never read off a request body at all — a caller who could set it would
be minting its own approval, which is precisely the failure this
subsystem exists to prevent.

UNTRUSTED TEXT (GUARDRAILS.md §6): `question` and `rules_text` come from
a venue payload. They are returned for a human to READ and are never
executed, evaluated, or interpreted as instructions by anything on this
path.
"""
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import AsyncSessionDep, MarketDataAdaptersDep
from app.models.event_link import EventLink
from app.services.matching import persist_proposals, propose_links
from app.strategies.base import normalize_outcome
from app.utils.time import utcnow
from app.venues.base import MarketDataAdapter, VenueError
from app.venues.types import VenueId, VenueMarket

router = APIRouter()

#: The review lifecycle, mirroring `app.models.event_link.EventLinkStatus`.
LinkStatus = Literal["proposed", "approved", "rejected"]

#: Fields shown side by side on `GET /links/{id}`, in this order. Order
#: is part of the contract: a reviewer compares two markets by scanning
#: the same row twice, so the fields must line up. `rules_text` is last
#: because it is the longest and the most important — everything above
#: it is context for reading it.
_COMPARED_FIELDS: tuple[str, ...] = (
    "venue",
    "market_id",
    "question",
    "close_time",
    "expected_settle_time",
    "resolution_source",
    "outcomes",
    "status",
    "rules_text",
)


class LinkOut(BaseModel):
    """One persisted `event_links` row."""

    id: int
    venue_a: str
    market_a: str
    venue_b: str
    market_b: str
    outcome_map: dict[str, str]
    confidence: float
    evidence: dict[str, Any]
    status: str
    reviewed_by: str | None
    reviewed_at: datetime | None
    notes: str | None
    created_at: datetime | None
    updated_at: datetime | None


class LinkListResponse(BaseModel):
    """Response for `GET /links`."""

    links: list[LinkOut]


class MarketSideOut(BaseModel):
    """One side of the review comparison.

    `close_time_iso` repeats `close_time` as an explicit ISO-8601 string
    WITH its UTC offset. A reviewer's most common real finding is a
    timezone-cutoff mismatch (ET midnight vs UTC midnight), and that is
    only visible if the offset is actually rendered rather than left to
    a client's date formatter.
    """

    venue: str
    market_id: str
    question: str
    outcomes: list[str]
    close_time: datetime
    close_time_iso: str
    expected_settle_time: datetime | None
    resolution_source: str | None
    status: str
    rules_text: str


class FieldComparison(BaseModel):
    """One field of the two markets, rendered for side-by-side reading."""

    field: str
    a: str | None
    b: str | None
    same: bool


class LinkReviewResponse(BaseModel):
    """Response for `GET /links/{id}` — the review surface.

    Attributes:
        link: The persisted row, including the matcher's evidence.
        market_a: Live metadata for the A side, or `None` if the venue
            no longer serves that market.
        market_b: Same for the B side.
        comparison: The two markets field by field, same fields in the
            same order, each row flagged `same`.
        warnings: Things a reviewer must know before deciding — a side
            that could not be read, or the reminder that this link is
            not cleared to trade.
    """

    link: LinkOut
    market_a: MarketSideOut | None
    market_b: MarketSideOut | None
    comparison: list[FieldComparison]
    warnings: list[str]


class ProposeResponse(BaseModel):
    """Response for `POST /links/propose`.

    Attributes:
        scanned_a: How many open markets were read from the first venue.
        scanned_b: How many from the second.
        created: New proposals written.
        updated: Existing UNREVIEWED proposals rescored in place.
        skipped_reviewed: Pairs the matcher proposed again that a human
            has already decided. These are left EXACTLY as the reviewer
            left them — see `POST /links/propose`'s docstring.
        links: Every row this call touched, plus the reviewed ones it
            declined to touch.
    """

    scanned_a: int
    scanned_b: int
    created: int
    updated: int
    skipped_reviewed: int
    links: list[LinkOut]


class ApproveRequest(BaseModel):
    """Body for `POST /links/{id}/approve`.

    `outcome_map` is optional ONLY because a binary-to-binary pair
    already carries the matcher's derived `{"YES": "YES", "NO": "NO"}`.
    For any other pair the matcher stores `{}` (it will not guess a
    correspondence between two venues' outcome labels), and approving
    with nothing to fall back on is a 422 — an approved link with no
    outcome map would build legs whose position ids match nothing (T18).
    """

    reviewed_by: str = Field(min_length=1, max_length=100)
    notes: str = Field(default="", max_length=4000)
    outcome_map: dict[str, str] | None = None


class RejectRequest(BaseModel):
    """Body for `POST /links/{id}/reject`.

    `notes` is where the reason lives, and it is the most valuable text
    in this table: "Kalshi settles on the AP call, Polymarket on state
    certification" is knowledge no score can rediscover.
    """

    reviewed_by: str = Field(min_length=1, max_length=100)
    notes: str = Field(default="", max_length=4000)


def _link_out(row: EventLink) -> LinkOut:
    """Build a `LinkOut` field by field from a persisted row."""
    return LinkOut(
        id=row.id,
        venue_a=row.venue_a,
        market_a=row.market_a,
        venue_b=row.venue_b,
        market_b=row.market_b,
        outcome_map=dict(row.outcome_map or {}),
        confidence=row.confidence,
        evidence=dict(row.evidence or {}),
        status=row.status,
        reviewed_by=row.reviewed_by,
        reviewed_at=row.reviewed_at,
        notes=row.notes,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _market_side(market: VenueMarket) -> MarketSideOut:
    """Build one side of the comparison from a live `VenueMarket`."""
    return MarketSideOut(
        venue=market.venue,
        market_id=market.market_id,
        question=market.question,
        outcomes=list(market.outcomes),
        close_time=market.close_time,
        close_time_iso=market.close_time.isoformat(),
        expected_settle_time=market.expected_settle_time,
        resolution_source=market.resolution_source,
        status=market.status,
        rules_text=market.rules_text,
    )


def _compare_value(side: MarketSideOut | None, name: str) -> str | None:
    """Render one field of one side as a comparable string."""
    if side is None:
        return None
    if name == "close_time":
        return side.close_time_iso
    if name == "expected_settle_time":
        settle = side.expected_settle_time
        return settle.isoformat() if settle is not None else None
    if name == "outcomes":
        return ", ".join(side.outcomes)
    value = getattr(side, name, None)
    return value if isinstance(value, str) else None


def _comparison(
    side_a: MarketSideOut | None, side_b: MarketSideOut | None
) -> list[FieldComparison]:
    """Render both markets field by field, in `_COMPARED_FIELDS` order.

    Args:
        side_a: The A market, or `None` if it could not be read.
        side_b: The B market, or `None`.

    Returns:
        list[FieldComparison]: One row per compared field. `same` is
            `False` whenever either side is missing — an unreadable side
            is not agreement.
    """
    rows: list[FieldComparison] = []
    for name in _COMPARED_FIELDS:
        left = _compare_value(side_a, name)
        right = _compare_value(side_b, name)
        rows.append(
            FieldComparison(
                field=name,
                a=left,
                b=right,
                same=side_a is not None and side_b is not None and left == right,
            )
        )
    return rows


def _canonical_outcome_map(raw: dict[str, str]) -> dict[str, str]:
    """Canonicalize a reviewer-supplied outcome map.

    Keys and values pass through `app.strategies.base.normalize_outcome`,
    which folds every spelling of yes/no onto `"YES"`/`"NO"` and passes
    other names through. Polymarket's Gamma payload spells its outcomes
    `"Yes"`/`"No"` while Kalshi's adapter forces `"YES"`/`"NO"`, and a
    position id is `f"{venue}:{market_id}:{outcome}"` — case-sensitive.
    A map stored as `{"Yes": "YES"}` would therefore build a leg whose
    position matched nothing and marked at its entry price forever.

    Args:
        raw: The map as the reviewer typed it.

    Returns:
        dict[str, str]: The canonicalized map.

    Raises:
        HTTPException: 422 if any key or value is blank after stripping.
    """
    canonical: dict[str, str] = {}
    for key, value in raw.items():
        if not key.strip() or not value.strip():
            raise HTTPException(
                status_code=422,
                detail="outcome_map keys and values must be non-empty outcome names",
            )
        canonical[normalize_outcome(key)] = normalize_outcome(value)
    return canonical


async def _read_market(
    adapters: dict[VenueId, MarketDataAdapter], venue: str, market_id: str
) -> VenueMarket | None:
    """Read one market for review, or `None` if the venue cannot serve it.

    A link outlives the market it points at (a market closes, a venue
    delists it, a ticker is reissued), and a reviewer looking at a
    half-readable link is better served by a warning than by a 500.

    Args:
        adapters: Read-only adapters keyed by venue.
        venue: The venue recorded on the link.
        market_id: The venue-native market id recorded on the link.

    Returns:
        VenueMarket | None: The market, or `None` if the venue is not
            wired or no longer serves that id.
    """
    # `venue` comes off a persisted row, so it is a plain `str` and may
    # name a venue this process does not wire (a third venue added later,
    # or a row written by an older build). Matched by iteration rather
    # than `dict.get` so the narrowing is explicit instead of a cast.
    adapter = next(
        (wired for known, wired in adapters.items() if known == venue), None
    )
    if adapter is None:
        return None
    try:
        return await adapter.get_market(market_id)
    except (KeyError, VenueError):
        return None


@router.get("")
async def list_links(
    session: AsyncSessionDep,
    status: LinkStatus | None = Query(
        default=None, description="Filter by review lifecycle."
    ),
) -> LinkListResponse:
    """List event links, optionally filtered by review lifecycle.

    `?status=proposed` is the review queue; `?status=approved` is the
    set T18's cross-venue strategy is allowed to consume.

    Args:
        session: Database session.
        status: Restrict to one lifecycle value, if given.

    Returns:
        LinkListResponse: Matching links, most confident first.
    """
    query = select(EventLink).order_by(EventLink.confidence.desc(), EventLink.id)
    if status is not None:
        query = query.where(EventLink.status == status)
    rows = (await session.execute(query)).scalars().all()
    return LinkListResponse(links=[_link_out(row) for row in rows])


@router.post("/propose")
async def propose(
    session: AsyncSessionDep,
    adapters: MarketDataAdaptersDep,
    min_confidence: float = Query(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Inclusive score floor for writing a proposal.",
    ),
) -> ProposeResponse:
    """Run the deterministic matcher over both venues and file proposals.

    Reads every OPEN market from each venue's read-only adapter, scores
    the blocked candidate pairs, and writes each pair scoring at or above
    `min_confidence` as a `"proposed"` link.

    A RE-RUN NEVER OVERWRITES A HUMAN. If a pair already has a row that a
    reviewer has decided, this leaves it completely alone — lifecycle,
    outcome map, notes, reviewer, timestamp — and counts it in
    `skipped_reviewed`. Rescoring a decided link would silently discard
    the one piece of information in this table a machine cannot
    reproduce: a person's reading of two rules texts. Unreviewed
    proposals ARE refreshed in place, so a moved close time or an edited
    question updates the queue rather than duplicating it.

    That reconciliation is `app.services.matching.persist
    .persist_proposals`, not a loop written here, because since T30 this
    endpoint is no longer the only caller: `app.tasks.matching` runs the
    same matcher on a beat. One implementation of "which rows a re-run
    may touch" is the point — two copies would eventually let the
    automatic caller do something this manual one refuses to.

    Args:
        session: Database session.
        adapters: Read-only market-data adapters, one per venue. These
            cannot place an order (see `app.api.deps.
            get_market_data_adapters`).
        min_confidence: Inclusive score floor. Lowering it widens the
            REVIEW queue; it does not widen what may trade.

    Returns:
        ProposeResponse: Counts plus every row involved.
    """
    markets_a = await adapters["polymarket"].list_markets(status="open")
    markets_b = await adapters["kalshi"].list_markets(status="open")
    proposals = propose_links(markets_a, markets_b, min_confidence=min_confidence)

    outcome = await persist_proposals(session, proposals)

    # `persist_proposals` commits but does not refresh: only a caller
    # rendering a response needs the server-side `created_at`/`updated_at`
    # back, and the beat (which touches every pair on both venues) must
    # not pay a SELECT per row for timestamps it never reads.
    for row in outcome.rows:
        await session.refresh(row)

    return ProposeResponse(
        scanned_a=len(markets_a),
        scanned_b=len(markets_b),
        created=outcome.created,
        updated=outcome.updated,
        skipped_reviewed=outcome.skipped_reviewed,
        links=[_link_out(row) for row in outcome.rows],
    )


@router.get("/{link_id}")
async def get_link(
    link_id: int, session: AsyncSessionDep, adapters: MarketDataAdaptersDep
) -> LinkReviewResponse:
    """Return one link with both markets rendered side by side for review.

    This is the surface a reviewer decides on. Both markets' `rules_text`,
    `close_time` (with offset), `resolution_source`, `outcomes` and
    lifecycle are returned in the same fields in the same order, plus a
    per-field `comparison` flagging what differs — because the finding
    that matters is usually a wording difference in the resolution rules,
    which no score in `evidence` can surface.

    Args:
        link_id: Primary key of the `event_links` row.
        session: Database session.
        adapters: Read-only market-data adapters, one per venue.

    Returns:
        LinkReviewResponse: The row, both sides, the comparison, and any
            warnings.

    Raises:
        HTTPException: 404 if no such link.
    """
    row = await session.get(EventLink, link_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"link {link_id} not found")

    market_a = await _read_market(adapters, row.venue_a, row.market_a)
    market_b = await _read_market(adapters, row.venue_b, row.market_b)
    side_a = _market_side(market_a) if market_a is not None else None
    side_b = _market_side(market_b) if market_b is not None else None

    warnings: list[str] = []
    if side_a is None:
        warnings.append(
            f"market A ({row.venue_a}:{row.market_a}) could not be read from its "
            "venue; the comparison below is incomplete"
        )
    if side_b is None:
        warnings.append(
            f"market B ({row.venue_b}:{row.market_b}) could not be read from its "
            "venue; the comparison below is incomplete"
        )
    if row.status != "approved":
        warnings.append(
            f"this link is {row.status!r}: nothing may trade on it until a human "
            "approves it (PLAN.md D9)"
        )
    if not row.outcome_map:
        warnings.append(
            "the matcher could not derive an outcome map for this pair; supply one "
            "in the approve request"
        )

    return LinkReviewResponse(
        link=_link_out(row),
        market_a=side_a,
        market_b=side_b,
        comparison=_comparison(side_a, side_b),
        warnings=warnings,
    )


@router.post("/{link_id}/approve")
async def approve_link(
    link_id: int, request: ApproveRequest, session: AsyncSessionDep
) -> LinkOut:
    """Record a human's approval of one event link.

    The only transition in this codebase that clears a link to trade, and
    the reason `app/services/matching/` is structurally unable to make
    it. `reviewed_by`/`reviewed_at`/`notes` are written here and only
    here.

    Args:
        link_id: Primary key of the `event_links` row.
        request: Reviewer, notes, and an optional outcome map that
            overrides whatever the row carries.
        session: Database session.

    Returns:
        LinkOut: The updated row.

    Raises:
        HTTPException: 404 if no such link; 422 if the link would end up
            approved with an EMPTY outcome map — which happens on every
            multi-outcome pair, where the matcher deliberately refuses to
            guess the correspondence between two venues' outcome labels.
            An approved link with no map is worse than no link: T18 would
            build legs whose `f"{venue}:{market_id}:{outcome}"` position
            ids match nothing.
    """
    row = await session.get(EventLink, link_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"link {link_id} not found")

    supplied = request.outcome_map
    outcome_map = _canonical_outcome_map(
        supplied if supplied else dict(row.outcome_map or {})
    )
    if not outcome_map:
        raise HTTPException(
            status_code=422,
            detail=(
                f"link {link_id} has no outcome_map and none was supplied; a "
                "multi-outcome pair must be mapped explicitly before approval"
            ),
        )

    row.outcome_map = outcome_map
    row.status = "approved"
    row.reviewed_by = request.reviewed_by
    row.reviewed_at = utcnow()
    row.notes = request.notes
    await session.commit()
    await session.refresh(row)
    return _link_out(row)


@router.post("/{link_id}/reject")
async def reject_link(
    link_id: int, request: RejectRequest, session: AsyncSessionDep
) -> LinkOut:
    """Record a human's rejection of one event link.

    No outcome map is required: a rejected link is never traded, and the
    reviewer's `notes` are the durable output — the reason two contracts
    that scored alike are not the same bet.

    Args:
        link_id: Primary key of the `event_links` row.
        request: Reviewer and notes.
        session: Database session.

    Returns:
        LinkOut: The updated row.

    Raises:
        HTTPException: 404 if no such link.
    """
    row = await session.get(EventLink, link_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"link {link_id} not found")

    row.status = "rejected"
    row.reviewed_by = request.reviewed_by
    row.reviewed_at = utcnow()
    row.notes = request.notes
    await session.commit()
    await session.refresh(row)
    return _link_out(row)
