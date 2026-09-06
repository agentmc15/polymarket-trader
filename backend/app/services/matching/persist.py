"""Filing matcher proposals into `event_links` (T30, PLAN.md D9).

`app.services.matching.matcher.propose_links` returns UNSAVED rows and
deliberately says nothing about how they meet the database. This module
is that other half, and it exists because there are now TWO callers with
one rule between them: the human-driven `POST /links/propose`
(`app/api/routes/links.py`) and the periodic `app.tasks.matching` beat.
Before the beat existed, the reconciliation rules lived inline in the
route; two copies of "which rows a re-run may touch" is exactly the kind
of divergence that would eventually let the automatic caller do
something the manual one refuses to.

A RE-RUN NEVER OVERWRITES A HUMAN. The four cases, in full, because a
periodic caller hits all of them every interval forever:

1. **No row for the pair yet** -> INSERT one, `"proposed"`.
2. **An undecided `"proposed"` row** -> rescored IN PLACE (confidence,
   evidence, outcome map). The queue then reflects the markets as they
   read today rather than accumulating a second row per interval.
3. **A row a human APPROVED** -> untouched, counted in
   `skipped_reviewed`. Rescoring it would silently move the ground under
   a decision a person made by reading two rules texts, and demoting it
   would be this package writing a lifecycle value it may not write.
4. **A row a human REJECTED** -> untouched, same as (3), and this is the
   case that matters most for an automatic caller. The matcher will
   propose a rejected pair again on every single pass — its score has not
   changed and nothing in the score can see the reason for the
   rejection ("Kalshi settles on the AP call, Polymarket on state
   certification"). A proposer that re-filed it, or reset it to
   `"proposed"`, would make rejection meaningless and train the operator
   to ignore the review queue, which is the one control PLAN.md R1
   depends on.

Rows are matched on the UNIQUE `(venue_a, market_a, venue_b, market_b)`
tuple that `propose_links` already puts in canonical order, so a pair
cannot re-enter the queue by arriving from the other venue's list first.

CASE 3 AND CASE 4 HOLD UNDER CONCURRENCY, NOT MERELY IN ORDER (T37). A
pass reads a row, judges it undecided, and writes it — and a human can
approve that same row in between, because the reviewer's approval is a
different transaction on a different connection and a real pass carries
hundreds of proposals between the read and the commit. Checking
`is_decided` on a Python object loaded earlier is therefore a check of
what WAS true, not of what is true when the UPDATE lands. So the check is
also carried IN THE UPDATE ITSELF: `update_proposal` builds
`... WHERE id = :id AND status = 'proposed' AND reviewed_at IS NULL`, and
`persist_proposals` reads `rowcount` — zero means a human got there
first, and the pair is counted in `skipped_reviewed` exactly as if the
decision had already been visible at read time.

A statement-level precondition rather than `SELECT ... FOR UPDATE` or a
`version_id_col`, deliberately: it behaves identically on Postgres
(production) and SQLite (the test engine, where `FOR UPDATE` parses and
then does nothing), so the guarantee is exercised by the same test that
runs in CI. It costs one thing worth naming: the row lock is taken when
the UPDATE executes rather than at commit, so a reviewer approving a row
this pass has already rescored blocks until the pass commits. The pass
does no I/O beyond the database once it starts filing, so that wait is
short — and blocking is the correct behaviour anyway, since the
alternative is the two writes racing.

THIS MODULE WRITES EXACTLY ONE LIFECYCLE VALUE, `PROPOSED`. It cannot
express another: the only status assignment is on a freshly built row,
`update_proposal` never touches `status`/`reviewed_by`/`reviewed_at` (it
only READS them, as the precondition), and `persist_proposals` REFUSES a
proposal that arrives carrying any other value (`ValueError`) rather than
trusting its caller. Promotion is a human act performed through
`app/api/routes/links.py` — see `app.models.event_link` and PLAN.md
D9/R1 for why that separation is structural rather than stylistic.
"""
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Update, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event_link import EventLink

logger = logging.getLogger(__name__)

#: The one `app.models.event_link.EventLinkStatus` value anything in
#: `app/services/matching/` may write. Named rather than repeated as a
#: literal so the grep that keeps this package honest has one place to
#: land.
PROPOSED = "proposed"


@dataclass(frozen=True)
class ProposalOutcome:
    """What one `persist_proposals` call did.

    Attributes:
        created: New `"proposed"` rows inserted.
        updated: Existing UNDECIDED proposals rescored in place.
        skipped_reviewed: Pairs the matcher proposed again that a human
            has already approved or rejected. Left EXACTLY as the
            reviewer left them. Includes the pairs a human decided
            DURING this pass, whose guarded UPDATE matched no row.
        rows: Every row the call touched, plus the decided ones it
            declined to touch, in proposal order.

    Note:
        `rows` are live ORM objects on the caller's session, and since
        T37 the rescore is issued as a guarded UPDATE statement rather
        than as an attribute assignment, so a row's in-memory attributes
        hold what the pass READ, not what it wrote. A caller rendering
        them (the API route, building a response) must `refresh` each row
        — which it already does for the server-side
        `created_at`/`updated_at`; a caller that only needs the counters
        (the Celery beat) must not touch them at all.
    """

    created: int
    updated: int
    skipped_reviewed: int
    rows: list[EventLink]

    @property
    def considered(self) -> int:
        """Total proposals the call looked at.

        Returns:
            int: `created + updated + skipped_reviewed`.
        """
        return self.created + self.updated + self.skipped_reviewed


def is_decided(row: EventLink) -> bool:
    """Whether a persisted link carries a human's decision.

    BOTH signals are checked, not just `status`: `reviewed_by`/
    `reviewed_at` are written only by `app/api/routes/links.py`, so a row
    carrying a `reviewed_at` has been in front of a person whatever its
    current status says. Treating such a row as undecided because its
    status happens to read `"proposed"` is the failure this function
    exists to make impossible.

    This answers the question ABOUT A LOADED PYTHON OBJECT, so its answer
    is only as fresh as the SELECT that produced it. It is the cheap
    skip, not the guarantee: the guarantee is the identical predicate
    carried in `update_proposal`'s `WHERE`, which the database evaluates
    against the row as it is when the UPDATE executes.

    Args:
        row: A persisted `event_links` row.

    Returns:
        bool: `True` if a human had decided this pair as of the read that
            loaded it, and the proposer must leave it alone.
    """
    return row.status != PROPOSED or row.reviewed_at is not None


def new_proposal(proposal: EventLink) -> EventLink:
    """Build the row to INSERT for a pair with no row yet.

    Field by field from a validated proposal, never
    `EventLink(**vars(proposal))` — see `app.models.event_link`'s
    mass-assignment writeup. `status` is the constant `PROPOSED` and is
    never copied from the input, so even a proposal that somehow carried
    another value (it cannot; `persist_proposals` rejects it first) could
    not launder it into the table.

    Args:
        proposal: An unsaved row from `propose_links`.

    Returns:
        EventLink: A new unsaved row, `"proposed"`, unreviewed.
    """
    return EventLink(
        venue_a=proposal.venue_a,
        market_a=proposal.market_a,
        venue_b=proposal.venue_b,
        market_b=proposal.market_b,
        outcome_map=dict(proposal.outcome_map),
        confidence=proposal.confidence,
        evidence=dict(proposal.evidence),
        status=PROPOSED,
    )


def update_proposal(existing: EventLink, proposal: EventLink) -> Update:
    """Build the GUARDED update that rescores one undecided proposal.

    A statement rather than an attribute assignment, because the point of
    this function is the `WHERE` (T37). Assigning
    `existing.confidence = ...` would make the decidedness test and the
    write two different transactions separated by the rest of the pass; a
    human approving in that window would be silently overwritten, and the
    haircut `app.strategies.cross_venue_arbitrage` applies as
    `p_same_resolution` would be the machine's number rather than the one
    the reviewer accepted. Carrying `status = 'proposed' AND reviewed_at
    IS NULL` in the statement makes the database re-check decidedness at
    the moment of the write, so a decision made mid-pass wins.

    Only the three derived fields move: a venue that edits a question or
    moves a close time changes the score, and the review queue should
    show today's score rather than the one from whenever the pair was
    first seen. `status`, `reviewed_by`, `reviewed_at` and `notes` appear
    only as the precondition READ; nothing here assigns them.

    Args:
        existing: The persisted row, as this session read it. Only its
            `id` is used — every value that decides whether the write
            happens is re-read by the database.
        proposal: The freshly scored proposal for the same pair.

    Returns:
        Update: The guarded statement. Its `rowcount` is 1 when the row
            was still undecided and 0 when a human decided it first.
    """
    return (
        update(EventLink)
        .where(
            EventLink.id == existing.id,
            EventLink.status == PROPOSED,
            EventLink.reviewed_at.is_(None),
        )
        .values(
            confidence=proposal.confidence,
            evidence=dict(proposal.evidence),
            outcome_map=dict(proposal.outcome_map),
        )
    )


async def _find_existing(
    session: AsyncSession, proposal: EventLink
) -> EventLink | None:
    """Return the persisted row for a proposal's pair, if there is one.

    One indexed lookup per proposal (`ix_event_links_venue_a_market_a`
    plus the unique constraint) rather than one bulk pre-load of the
    whole table: the table outlives the markets in it and grows with
    every pair ever reviewed, while a single pass only ever asks about
    the pairs it actually proposed.

    Args:
        session: Database session.
        proposal: The unsaved proposal, in canonical column order.

    Returns:
        EventLink | None: The existing row, or `None`.
    """
    return (
        await session.execute(
            select(EventLink).where(
                EventLink.venue_a == proposal.venue_a,
                EventLink.market_a == proposal.market_a,
                EventLink.venue_b == proposal.venue_b,
                EventLink.market_b == proposal.market_b,
            )
        )
    ).scalar_one_or_none()


async def persist_proposals(
    session: AsyncSession, proposals: Sequence[EventLink]
) -> ProposalOutcome:
    """File a matcher run's proposals, never overwriting a human's decision.

    Idempotent by construction: running it twice over unchanged markets
    creates nothing the second time, and running it forever on a beat
    can neither duplicate a queued proposal nor resurrect a rejected one.
    See this module's docstring for the four cases in full.

    Commits once at the end, so a pass is all-or-nothing: a partially
    filed queue is harder to reason about than an empty one, and the next
    interval refiles it.

    Safe against a human deciding a row MID-PASS: every rescore is a
    guarded UPDATE that re-checks `status`/`reviewed_at` in the database
    at the moment it writes (see `update_proposal` and this module's
    docstring). A pair decided between this pass's read and its write is
    counted in `skipped_reviewed`, not overwritten.

    Args:
        session: Database session. Committed by this function.
        proposals: Unsaved rows from
            `app.services.matching.matcher.propose_links`.

    Returns:
        ProposalOutcome: The counters and the rows involved.

    Raises:
        ValueError: If any proposal arrives with a lifecycle value other
            than `PROPOSED`, or already carrying a reviewer. Nothing this
            package produces can, and refusing here means a future caller
            cannot use this function to write an approval either.
        sqlalchemy.exc.IntegrityError: If the commit races another writer
            inserting the same pair. The caller decides what that means;
            `app.tasks.matching` rolls back and lets the next interval
            refile, since nothing here is lost by being deferred.
    """
    created = 0
    updated = 0
    skipped_reviewed = 0
    touched: list[EventLink] = []

    for proposal in proposals:
        if proposal.status != PROPOSED or proposal.reviewed_by is not None:
            raise ValueError(
                "persist_proposals only files proposals: refusing a row with "
                f"status={proposal.status!r} reviewed_by={proposal.reviewed_by!r} "
                "(PLAN.md D9 — approval is a human act made through "
                "app/api/routes/links.py)"
            )

        existing = await _find_existing(session, proposal)
        if existing is None:
            row = new_proposal(proposal)
            session.add(row)
            created += 1
            touched.append(row)
            continue

        if is_decided(existing):
            skipped_reviewed += 1
            touched.append(existing)
            continue

        # `synchronize_session=False`: the guarded statement is the write
        # of record, and letting the ORM also mirror it onto `existing`
        # would put the pass's numbers on a row the database may have
        # refused to change. `rows` therefore carry what was READ — see
        # `ProposalOutcome.rows`.
        result = await session.execute(
            update_proposal(existing, proposal),
            execution_options={"synchronize_session": False},
        )
        if result.rowcount:
            updated += 1
        else:
            # A human approved or rejected this pair between this pass's
            # SELECT above and this UPDATE. Their decision stands; the
            # pair is skipped exactly as if it had already been decided
            # when the pass read it.
            skipped_reviewed += 1
            logger.info(
                "link_proposal",
                extra={
                    "event": "link_rescore_declined_decided_row",
                    "link_id": existing.id,
                    "venue_a": existing.venue_a,
                    "market_a": existing.market_a,
                    "venue_b": existing.venue_b,
                    "market_b": existing.market_b,
                },
            )
        touched.append(existing)

    await session.commit()
    return ProposalOutcome(
        created=created,
        updated=updated,
        skipped_reviewed=skipped_reviewed,
        rows=touched,
    )
