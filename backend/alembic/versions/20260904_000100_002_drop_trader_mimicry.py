"""Drop trader mimicry (copy-trading "whales").

Revision ID: 002
Revises: 001
Create Date: 2026-09-04 00:01:00.000000+00:00

Rationale (PLAN.md D2): on prediction markets a single wallet's realized
trades are far too few, too correlated (same events, same news), and too
survivorship-selected (leaderboards show winners after the fact) to
distinguish skill from variance; a copier also pays the latency and
slippage the leader did not. It is not sustainable, so copy-trading is
removed outright rather than excised gradually.

Repo state made deletion cheap: only `tracked_traders` ever received a
migration (in `001`); `traders` and `trader_follows` never existed in any
database — they were declared as SQLAlchemy models
(`app/models/trader.py`) but never migrated. This migration drops
`tracked_traders` for real, and issues `DROP TABLE IF EXISTS` for the two
never-created tables so that a stray dev database (one that was, for
whatever reason, created directly from `Base.metadata.create_all` rather
than via Alembic) is left clean too.

`TradeHistory` (the public trade tape, used by `data_replay` and
`backfill_data`) is NOT mimicry and is unaffected by this migration.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop tracked_traders (mirrors 001's downgrade block for this table)
    op.drop_index("ix_tracked_traders_active_pnl", table_name="tracked_traders")
    op.drop_index("ix_tracked_traders_win_rate", table_name="tracked_traders")
    op.drop_index("ix_tracked_traders_pnl", table_name="tracked_traders")
    op.drop_index("ix_tracked_traders_is_active", table_name="tracked_traders")
    op.drop_index("ix_tracked_traders_address", table_name="tracked_traders")
    op.drop_table("tracked_traders")

    # traders / trader_follows never received a migration (only ever
    # created ad hoc via Base.metadata.create_all on a dev database), so
    # there is no corresponding op.create_table to reverse — IF EXISTS
    # guards against both "table was never created" and "already dropped".
    op.execute("DROP TABLE IF EXISTS trader_follows")
    op.execute("DROP TABLE IF EXISTS traders")


def downgrade() -> None:
    # Recreate tracked_traders exactly as 001 does.
    op.create_table(
        "tracked_traders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("address", sa.String(42), nullable=False),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("total_pnl", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("win_rate", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("total_trades", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("copy_multiplier", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("address"),
    )
    op.create_index("ix_tracked_traders_address", "tracked_traders", ["address"])
    op.create_index("ix_tracked_traders_is_active", "tracked_traders", ["is_active"])
    op.create_index("ix_tracked_traders_pnl", "tracked_traders", ["total_pnl"])
    op.create_index("ix_tracked_traders_win_rate", "tracked_traders", ["win_rate"])
    op.create_index(
        "ix_tracked_traders_active_pnl", "tracked_traders", ["is_active", "total_pnl"]
    )
