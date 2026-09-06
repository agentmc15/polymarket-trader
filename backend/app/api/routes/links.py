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

CONCURRENT WRITERS (T37). Three writers reach `event_links`: this
module's two review routes, `POST /links/propose`, and the
`app.tasks.matching` beat — and the beat runs while a reviewer is
reading. Two consequences are handled here rather than left to
last-write-wins. `POST /links/{id}/approve` carries its own precondition
(`status = 'proposed' AND reviewed_at IS NULL`) IN the UPDATE, so an
approval that arrives after another reviewer has already decided the
same link is a `409`, not a silent overwrite of their decision;
`app.services.matching.persist` carries the mirror-image predicate so
the beat cannot overwrite an approval. And `POST /links/propose` treats
the beat inserting one of its pairs first as a `409` rather than an
unhandled `IntegrityError` — the same "nothing is lost by deferring"
reading `app.tasks.matching` takes of that race from its side.

`POST /links/{id}/reject` deliberately has NO such precondition. It is
the revocation path: taking an approved link back out of the trading set
is always allowed and always fail-safe, because a rejected link is one
`cross_venue_arbitrage` may not touch. The asymmetry is the point —
guard the transition that CLEARS capital to move, not the one that
stops it.

NOTES ARE APPEND-ONLY (T43). That asymmetry is about the LIFECYCLE
column and says nothing about `notes`, and until T43 `reject_link`
assigned `row.notes = request.notes` unconditionally — so a second
rejection, including a bare one carrying no note at all, replaced the
previous reviewer's reason with `""` and returned `200`. `notes` is the
one column here whose contents no re-run can rediscover, so rejection
now MERGES rather than overwrites (`_appended_notes`): existing text is
never shortened, a new note is appended under a header naming its author
and the time, and a rejection carrying no note leaves the text exactly
as it was. Rejection itself stays unconditionally permitted — nothing
here turns the fail-safe direction into a `409`.

A DECISION IS TERMINAL ON THIS API, and `approve_link`'s `409` detail
now says so. No route returns a link to `"proposed"`: `approve_link`
requires `status = 'proposed' AND reviewed_at IS NULL`, `reject_link`
only ever writes `"rejected"`, and `persist_proposals` skips decided
rows — so a link rejected against a mistyped `link_id` stays rejected
until someone edits the row in the database. The detail this replaced
told the operator to "re-read the link before deciding it again", naming
an action that does not exist.
"""
import logging
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.api.deps import AsyncSessionDep, MarketDataAdaptersDep
from app.models.event_link import EventLink
from app.services.matching import PROPOSED, persist_proposals, propose_links
from app.strategies.base import normalize_outcome
from app.utils.time import utcnow
from app.venues.base import MarketDataAdapter, VenueError
from app.venues.types import VenueId, VenueMarket

logger = logging.getLogger(__name__)

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

    #: T33: an unknown body key is a 422 naming the field, never a
    #: silently discarded value. See `app.api.routes.backtesting.
    #: BacktestRequest`'s docstring for the full rationale. It matters
    #: doubly here: this body is one half of a HUMAN REVIEW record, and
    #: a misspelled `reviewed_by`/`outcome_map` that vanished silently
    #: would attribute an approval to nobody, or approve a non-binary
    #: pair with no outcome map at all.
    model_config = ConfigDict(extra="forbid")

    reviewed_by: str = Field(min_length=1, max_length=100)
    notes: str = Field(default="", max_length=4000)
    outcome_map: dict[str, str] | None = None


class RejectRequest(BaseModel):
    """Body for `POST /links/{id}/reject`.

    `notes` is where the reason lives, and it is the most valuable text
    in this table: "Kalshi settles on the AP call, Polymarket on state
    certification" is knowledge no score can rediscover.

    It is therefore APPENDED to whatever the row already carries rather
    than replacing it (`_appended_notes`), and the default `""` means
    "I am withdrawing this link and have nothing to add", not "blank the
    previous reviewer's reason".
    """

    #: T33, same rule and same reason as `ApproveRequest` above: a
    #: misspelled `notes` key would throw away exactly the text this
    #: model exists to capture.
    model_config = ConfigDict(extra="forbid")

    reviewed_by: str = Field(min_length=1, max_length=100)
    notes: str = Field(default="", max_length=4000)


def _appended_notes(
    existing: str | None, incoming: str, reviewed_by: str, decided_at: datetime
) -> str | None:
    """Merge a rejecting reviewer's note into whatever the row carries.

    WHY APPEND RATHER THAN REFUSE-BLANK-OVER-NON-BLANK (T43 F2). Both
    stop the reproduced defect — a bare `POST /links/{id}/reject`
    blanking the previous reviewer's reason with a `200` — but refusing
    only the EMPTY overwrite closes half of it: a rejection carrying any
    note at all, even a one-word one, would still delete a paragraph
    somebody wrote after reading two rules texts, silently and with the
    same `200`. That is the same loss for the same reason. Appending
    closes both, and it costs nothing structurally: `EventLink.notes` is
    `Text`, so an accumulated history has no column bound (the 4000-char
    limit on `RejectRequest.notes` is per REQUEST), and rejection stays
    unconditionally permitted, which is the property `reject_link`'s
    docstring is built on. Nothing here can make a rejection fail.

    Each appended block carries a header naming its author and the time,
    because `reviewed_by`/`reviewed_at` hold only the LATEST decision —
    once a third rejection lands, that header is the only surviving
    record of who wrote the second note. The first note on a row is
    stored verbatim instead, so the ordinary one-reviewer row reads
    exactly as it always has and matches what `approve_link` writes.

    Args:
        existing: `EventLink.notes` as persisted, possibly `None`.
        incoming: The note on this request, possibly `""`.
        reviewed_by: Who is rejecting now — the appended block's author.
        decided_at: This decision's aware-UTC timestamp, the same value
            written to `reviewed_at`, so the two agree exactly.

    Returns:
        str | None: The merged note. Never shorter than `existing`, and
            `existing` unchanged whenever `incoming` is blank.
    """
    previous = (existing or "").strip()
    addition = incoming.strip()
    if not previous:
        # Nothing to lose: store what the reviewer typed, byte for byte,
        # including the `""` a bare rejection sends.
        return incoming
    if not addition:
        return existing
    return f"{previous}\n\n[{reviewed_by} @ {decided_at.isoformat()}] {addition}"


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

    Raises:
        HTTPException: 409 if the beat (or another operator) inserted one
            of these pairs between this call's lookup and its commit. The
            pass is rolled back whole and NOTHING is written — which
            costs nothing, because the matcher is deterministic and the
            row the other writer just filed is the row this call would
            have filed. `app.tasks.matching` reads the same race the same
            way from its side; an unhandled `IntegrityError` here would
            have been a 500 for a race with no consequences.
    """
    markets_a = await adapters["polymarket"].list_markets(status="open")
    markets_b = await adapters["kalshi"].list_markets(status="open")
    proposals = propose_links(markets_a, markets_b, min_confidence=min_confidence)

    try:
        outcome = await persist_proposals(session, proposals)
    except IntegrityError as exc:
        await session.rollback()
        logger.warning(
            "link_proposal",
            extra={
                "event": "link_proposal_conflict",
                "proposals": len(proposals),
            },
        )
        raise HTTPException(
            status_code=409,
            detail=(
                "another writer filed one of these pairs while this pass was "
                "running; nothing was written — retry"
            ),
        ) from exc

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

    Only a link that is still `"proposed"` and still unreviewed may be
    approved, and that precondition travels IN the UPDATE rather than
    being checked against the row read a moment earlier (T37). Two
    reviewers opening the same queue entry is ordinary, and the second
    one's approval must not quietly erase the first one's rejection — the
    note explaining WHY a pair is not the same bet ("Kalshi settles on
    the AP call, Polymarket on state certification") is the one thing in
    this table no machine can rediscover.

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
            ids match nothing. 409 if the link has already been decided,
            including by another reviewer between this request's read and
            its write.
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

    # Field by field from the validated model, never `**model_dump()`
    # (see this module's MASS ASSIGNMENT note) — and guarded, so the
    # decision this request believes it is making is the decision the
    # database is still waiting for.
    result = await session.execute(
        update(EventLink)
        .where(
            EventLink.id == link_id,
            EventLink.status == PROPOSED,
            EventLink.reviewed_at.is_(None),
        )
        .values(
            outcome_map=outcome_map,
            status="approved",
            reviewed_by=request.reviewed_by,
            reviewed_at=utcnow(),
            notes=request.notes,
        ),
        execution_options={"synchronize_session": False},
    )
    if not result.rowcount:
        decided = await session.get(EventLink, link_id, populate_existing=True)
        if decided is None:
            raise HTTPException(status_code=404, detail=f"link {link_id} not found")
        # Symmetric with `persist_proposals`' `link_rescore_declined_
        # decided_row` (T43). A machine losing this race already left a
        # server-side trace; a contested HUMAN decision — the rarer and
        # more interesting event of the two — left none at all, so a
        # reviewer reporting "my approval did not stick" was
        # unreconstructable from the logs.
        logger.warning(
            "link_review",
            extra={
                "event": "link_approval_declined_decided_row",
                "link_id": link_id,
                "attempted_by": request.reviewed_by,
                "decided_status": decided.status,
                "decided_by": decided.reviewed_by,
            },
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"link {link_id} is already {decided.status!r}, decided by "
                f"{decided.reviewed_by!r}; nothing was changed. A decision is "
                "terminal on this API — no route returns a link to 'proposed' — "
                "so a link decided in error has to be corrected in the database "
                "directly"
            ),
        )

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

    No `status` precondition either, unlike `approve_link` (T37). This is
    the revocation path: a link an earlier reviewer approved must stay
    rejectable the moment someone spots the settlement difference, and
    the result of winning that race is a link nothing may trade — the
    fail-safe side. Guarding it would mean an operator had to undo an
    approval before they could withdraw it, which is exactly backwards
    for the transition that STOPS capital moving.

    That argument covers the LIFECYCLE column and nothing else, which is
    why `notes` is now merged rather than assigned (T43 F2). "Durable
    output" was not true of a column any later rejection overwrote,
    including a bare one with no note in it; `_appended_notes` makes it
    true without adding a precondition, so rejection is still always
    permitted and still cannot fail on account of what a previous
    reviewer wrote.

    REJECTION IS TERMINAL. Nothing in this API moves a decided link back
    to `"proposed"`, so a rejection filed against a mistyped `link_id`
    bans that pair until the row is edited in the database. See this
    module's docstring; `approve_link`'s conflict detail says the same
    thing to whoever hits it.

    Args:
        link_id: Primary key of the `event_links` row.
        request: Reviewer and notes. `notes` is appended to the row's
            existing text, never substituted for it.
        session: Database session.

    Returns:
        LinkOut: The updated row.

    Raises:
        HTTPException: 404 if no such link.
    """
    row = await session.get(EventLink, link_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"link {link_id} not found")

    decided_at = utcnow()
    row.status = "rejected"
    row.reviewed_by = request.reviewed_by
    row.reviewed_at = decided_at
    row.notes = _appended_notes(
        row.notes, request.notes, request.reviewed_by, decided_at
    )
    await session.commit()
    await session.refresh(row)
    return _link_out(row)
