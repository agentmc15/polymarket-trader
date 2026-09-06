"""What a rejection may do to the previous reviewer's note (T43 F2).

THE FINDING. `POST /links/{id}/reject` carries no precondition of any
kind — deliberately, and for a good reason: rejection is the fail-safe
direction, so a link an earlier reviewer approved must stay withdrawable
the moment anyone spots the settlement difference (T37, and
`tests/matching/test_link_write_races.py` pins it). But that argument is
about the LIFECYCLE column, and until T43 the route also assigned
`row.notes = request.notes` unconditionally. `RejectRequest.notes`
defaults to `""`. So::

    POST /links/1/reject {"reviewed_by": "alice",
                          "notes": "Kalshi settles on the AP call, "
                                   "Polymarket on state certification"}  -> 200
    POST /links/1/reject {"reviewed_by": "bob"}                          -> 200
    # alice's note is now ""

with a `200` and no trace. The module docstring calls `notes` "the most
valuable text in this table ... knowledge no score can rediscover" and
`reject_link`'s own docstring calls it "the durable output". A column
that any later rejection blanks is not durable, and the loss is exactly
the one this whole subsystem exists to prevent: PLAN.md D9 makes event
equivalence a HUMAN decision precisely because two markets that score
alike can settle differently, and the sentence explaining how is the
only artifact of that decision. `confidence` can be recomputed from the
markets; "the AP call vs state certification" cannot be recomputed from
anything.

THE FIX IS APPEND, NOT REFUSE. `app.api.routes.links._appended_notes`
merges instead of assigning, so nothing shortens the text and rejection
still cannot fail. Refusing only an EMPTY overwrite would have closed
the bare-rejection case above and left the one below it open — a second
rejection carrying a one-word note would still have deleted alice's
paragraph, silently, with the same `200`. See `_appended_notes` for the
full argument.

The last two tests cover the OTHER half of `reject_link`'s contract, the
half a careless fix breaks: notes that can never be added to are as bad
as notes that can be destroyed, so a new reviewer's text must actually
land, and a first note must still be stored exactly as typed.

No network anywhere (GUARDRAILS.md §1.4): every route exercised here
reaches only the database. `GET /links`, never `GET /links/{id}`, for
readback — the review surface reads both venues through
`get_market_data_adapters`, and nothing here has any business building a
venue adapter.
"""
import logging

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event_link import EventLink
from app.services.matching import PROPOSED

#: The note the finding is about, quoted from the module docstring of
#: `app/api/routes/links.py`. Kept as a constant so a test asserting it
#: survived is asserting the same text that was written.
AP_CALL_NOTE = (
    "Kalshi settles on the AP call, Polymarket on state certification"
)


async def _proposed_link(session: AsyncSession, market: str = "KX-FED") -> EventLink:
    """File one `"proposed"` link and return it.

    Built field by field rather than through the matcher: these tests are
    about what the REVIEW routes do to `notes`, and a literal row keeps
    the before/after text in the assertions readable.

    Args:
        session: Database session.
        market: Kalshi-side market id, so several links can coexist in
            one test without colliding on `uq_event_links_pair`.

    Returns:
        EventLink: The committed row, with `notes` still `None`.
    """
    row = EventLink(
        venue_a="kalshi",
        market_a=market,
        venue_b="polymarket",
        market_b=f"PM-{market}",
        outcome_map={"YES": "YES", "NO": "NO"},
        confidence=0.60,
        evidence={},
        status=PROPOSED,
    )
    session.add(row)
    await session.commit()
    return row


async def _notes(client: AsyncClient, link_id: int) -> str:
    """Read one link's `notes` back through the API."""
    listing = (await client.get("/api/v1/links")).json()["links"]
    row = next(link for link in listing if link["id"] == link_id)
    return str(row["notes"])


async def test_a_bare_rejection_does_not_blank_the_previous_note(
    client: AsyncClient, test_session: AsyncSession
) -> None:
    """The reproduction from the finding, asserted as the loss it is.

    Bob withdraws a link alice already rejected and types nothing —
    which is ordinary, because from bob's side the link is already out of
    the trading set and there is nothing to add. His empty `notes` must
    not be treated as an instruction to delete the only sentence in this
    table explaining WHY the pair is not one bet.
    """
    row = await _proposed_link(test_session)

    first = await client.post(
        f"/api/v1/links/{row.id}/reject",
        json={"reviewed_by": "alice", "notes": AP_CALL_NOTE},
    )
    assert first.status_code == 200, first.text
    assert first.json()["notes"] == AP_CALL_NOTE

    # No `notes` key at all — the default `""` is what did the damage.
    second = await client.post(
        f"/api/v1/links/{row.id}/reject", json={"reviewed_by": "bob"}
    )
    assert second.status_code == 200, second.text

    # The lifecycle still moves: rejection stays unconditionally
    # permitted, which is the property this fix must not have cost.
    assert second.json()["status"] == "rejected"
    assert second.json()["reviewed_by"] == "bob"

    # And the knowledge survives, byte for byte. A bare rejection has
    # nothing to append, so the text is not merely PRESENT, it is
    # untouched.
    assert await _notes(client, row.id) == AP_CALL_NOTE


async def test_rejecting_an_approved_link_keeps_the_approver_s_note(
    client: AsyncClient, test_session: AsyncSession
) -> None:
    """The same loss across a revocation, which is the worse direction.

    `reject_link` has no lifecycle precondition on purpose, so a bare
    rejection lands on an APPROVED link too — and the note it blanked was
    the record of the review that cleared capital to move on that pair.
    Losing it means nobody can later tell whether the approval was
    reasoned or careless.
    """
    row = await _proposed_link(test_session, market="KX-CPI")

    approved = await client.post(
        f"/api/v1/links/{row.id}/approve",
        json={"reviewed_by": "alice", "notes": "verified identical rules"},
    )
    assert approved.status_code == 200, approved.text

    withdrawn = await client.post(
        f"/api/v1/links/{row.id}/reject", json={"reviewed_by": "mallory"}
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["status"] == "rejected"
    assert withdrawn.json()["reviewed_by"] == "mallory"

    assert await _notes(client, row.id) == "verified identical rules"


async def test_two_rejections_with_notes_keep_both(
    client: AsyncClient, test_session: AsyncSession
) -> None:
    """Preserving the old note must not swallow the new one.

    This is the anti-freeze half of the contract and the reason the fix
    appends rather than refusing to overwrite: a guard that simply kept
    the first note would leave the second reviewer's reading of the same
    two rules texts nowhere at all — the same class of loss, one reviewer
    later. Both sentences must be readable afterwards, and the appended
    one must name its author, because `reviewed_by` holds only the latest
    decision and will be someone else's name by the time a third
    rejection lands.
    """
    row = await _proposed_link(test_session, market="KX-NFP")

    await client.post(
        f"/api/v1/links/{row.id}/reject",
        json={"reviewed_by": "alice", "notes": AP_CALL_NOTE},
    )
    second = await client.post(
        f"/api/v1/links/{row.id}/reject",
        json={
            "reviewed_by": "bob",
            "notes": "also a 4pm ET vs 5pm ET cutoff difference",
        },
    )
    assert second.status_code == 200, second.text

    merged = await _notes(client, row.id)
    assert AP_CALL_NOTE in merged, "the first reviewer's reason was destroyed"
    assert "4pm ET vs 5pm ET" in merged, "the second reviewer's note never landed"
    assert merged.startswith(AP_CALL_NOTE), "history must read oldest first"
    assert "[bob @ " in merged, "an appended note must name who wrote it"


async def test_a_first_note_is_stored_exactly_as_typed(
    client: AsyncClient, test_session: AsyncSession
) -> None:
    """One reviewer, one note, no decoration — unchanged by T43.

    A row reaches its first rejection with `notes = None` (nothing in
    `app/services/matching/` writes that column), so there is nothing to
    preserve and no history to disambiguate. The stored text is therefore
    byte-identical to what the reviewer typed, exactly as
    `approve_link` writes it, and an attribution header appears only once
    there is a second note for it to separate.
    """
    row = await _proposed_link(test_session, market="KX-GDP")

    rejected = await client.post(
        f"/api/v1/links/{row.id}/reject",
        json={"reviewed_by": "alice", "notes": AP_CALL_NOTE},
    )
    assert rejected.status_code == 200, rejected.text

    assert await _notes(client, row.id) == AP_CALL_NOTE
    assert "[alice" not in await _notes(client, row.id)


# ---------------------------------------------------------------------------
# A decision is terminal, and the conflict says so.
# ---------------------------------------------------------------------------


async def test_the_approve_conflict_does_not_promise_a_path_back(
    client: AsyncClient, test_session: AsyncSession
) -> None:
    """The `409` must not name an action that does not exist (T43).

    `approve_link` requires `status = 'proposed' AND reviewed_at IS
    NULL`, `reject_link` only ever writes `"rejected"`, and
    `persist_proposals` skips decided rows — so nothing in this API
    returns a link to `"proposed"`. The detail used to end "Re-read the
    link before deciding it again", which reads as an instruction to
    retry and cannot be followed: a rejection filed against a mistyped
    `link_id` bans that pair permanently. Telling the operator where the
    recourse actually is (the database) is the honest form.
    """
    row = await _proposed_link(test_session, market="KX-JOBS")

    rejected = await client.post(
        f"/api/v1/links/{row.id}/reject",
        json={"reviewed_by": "bob", "notes": AP_CALL_NOTE},
    )
    assert rejected.status_code == 200, rejected.text

    conflict = await client.post(
        f"/api/v1/links/{row.id}/approve",
        json={"reviewed_by": "alice", "notes": "looks the same to me"},
    )
    assert conflict.status_code == 409, conflict.text
    detail = conflict.json()["detail"]
    assert "already 'rejected'" in detail
    assert "terminal" in detail
    assert "database" in detail
    assert "deciding it again" not in detail, (
        "the message points at a retry the API cannot perform"
    )


async def test_a_contested_human_decision_leaves_a_server_side_trace(
    client: AsyncClient, test_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """Symmetry with `link_rescore_declined_decided_row` (T43).

    `app.services.matching.persist` logs when the BEAT loses a race to a
    human. The mirror case — one reviewer's approval declined because
    another already decided the link — logged nothing at all, so the
    rarer and more interesting of the two conflicts was the one with no
    server-side record. A reviewer reporting "my approval did not stick"
    was unreconstructable from the logs.
    """
    row = await _proposed_link(test_session, market="KX-PPI")

    await client.post(
        f"/api/v1/links/{row.id}/reject",
        json={"reviewed_by": "bob", "notes": AP_CALL_NOTE},
    )

    with caplog.at_level(logging.WARNING, logger="app.api.routes.links"):
        conflict = await client.post(
            f"/api/v1/links/{row.id}/approve",
            json={"reviewed_by": "alice", "notes": "looks the same to me"},
        )
    assert conflict.status_code == 409, conflict.text

    declined = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "link_approval_declined_decided_row"
    ]
    assert len(declined) == 1, "a contested human decision left no trace"
    assert declined[0].link_id == row.id  # type: ignore[attr-defined]
    assert declined[0].attempted_by == "alice"  # type: ignore[attr-defined]
    assert declined[0].decided_by == "bob"  # type: ignore[attr-defined]
    assert declined[0].decided_status == "rejected"  # type: ignore[attr-defined]
