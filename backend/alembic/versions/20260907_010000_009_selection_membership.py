"""Add selection_membership: per-market book-collection selection state.

Revision ID: 009
Revises: 008
Create Date: 2026-09-07 01:00:00.000000+00:00

mm-proveout T16 (Phase 2 review, part c). `DataCollector.collect_books`
writes a `BookSnapshot` row only for a market in that tick's `selected`
set (`select_quotable_markets`, T7, PLAN.md D1). Measured on Kalshi over
5 minutes (2026-09-07): `quotable` held at 684, `selected` held at 500,
but 20 markets (4.0%) left the selected set and 20 entered -- every
departure was a lost quotability, none were pushed out by the
`book_collection_top_n` cap. Over three weeks of a 60-second beat, a
market's `BookSnapshot` series will be full of holes from exactly this
churn, and nothing before this migration lets a reader (T11) tell "we
stopped selecting this market" from "the collector broke" -- both look
identical: no row.

`app.models.selection_membership.SelectionMembership` closes that gap
with ONE row per `(venue, market_id)` -- not one row per tick -- carrying
the CURRENT selection state plus the most recent transition timestamp in
each direction, upserted every tick the same check-then-update way
`BookSnapshot` already is. Its size is bounded by the number of DISTINCT
markets ever observed as selected on a venue, not by elapsed ticks; see
that module's docstring for the full design rationale, including what is
deliberately NOT captured (a full transition history, as opposed to only
the latest transition of each kind).

Offline-safe (GUARDRAILS.md §2): one `CREATE TABLE`, no `CREATE TYPE`, no
reflection, nothing that needs a live connection -- `alembic upgrade head
--sql` renders it whole. This is a NEW table in a NEW migration, not
folded into `008`: unlike `008`'s four columns (irreversible the moment
collection starts, because `008` had not been applied anywhere), a new
table can be added at any time without losing anything, and `008` itself
was already described as "not modified further" once T15 finished it.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "009"
down_revision: str | None = "008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "selection_membership",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("venue", sa.String(length=16), nullable=False),
        sa.Column("market_id", sa.String(length=128), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column(
            "last_selected_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "last_deselected_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.UniqueConstraint(
            "venue", "market_id", name="uq_selection_membership_venue_market"
        ),
        sa.CheckConstraint(
            "venue IN ('polymarket', 'kalshi')",
            name="ck_selection_membership_venue_valid",
        ),
    )


def downgrade() -> None:
    """Irreversible: drops the whole table and every selection-state row
    with it. `SelectionMembership` carries no fact reconstructable from
    any other table (`BookSnapshot`'s presence/absence is exactly the
    ambiguity this table exists to resolve), so there is nothing to
    preserve on the way down -- matching `007`/`008`'s downgrade
    convention of documenting the loss rather than pretending otherwise.
    """
    op.drop_table("selection_membership")
