"""Add volume, the fee in force, and (T15) the columns that cannot be
added later to book_snapshots.

Revision ID: 008
Revises: 007
Create Date: 2026-09-07 00:00:00.000000+00:00

mm-proveout T8 (PLAN.md D9). `app.models.book_snapshot.BookSnapshot` has
carried an observed order book -- bids, asks, tick_size, min_size -- but
nothing about how much of it was actually trading or what it cost to
cross it. Two gaps that block the forward replay (T11):

1. Kalshi's `PriceHistory` path -- the ONE existing source of a volume
   TIME SERIES on this venue -- is Polymarket-only
   (`app.services.data_collector.DataCollector.collect_price_snapshot`
   only ever writes Polymarket rows; nothing analogous exists for
   Kalshi). A snapshot's own `volume` column is therefore the only
   activity signal a Kalshi book snapshot can carry at all: the delta
   between two consecutive snapshots' `volume` is what tells
   `app.scripts.mm_replay_snapshots` (T11) that a fill actually could
   have happened, the same role `Candle.volume` plays for the
   retrospective Kalshi replay (T1/T2).
2. Polymarket's per-market `feeSchedule` changes over time
   (`app.venues.types.FeeSchedule`, `source="venue_schedule"`) -- the fee
   quoted at collection time is not necessarily the fee in force when the
   replay later reads the row. Recording `taker_fee_rate`/
   `maker_rebate_rate` alongside the book means a fee computed from that
   row is never stale relative to whatever the schedule became later.

`volume` is `app.venues.types.venue_volume(market)` on the LISTING
payload the caller already fetched -- never re-derived from `raw` a
second way, for the reason `_book_collection_volume`'s docstring in
`app/services/data_collector.py` gives (a local re-read of `raw["volume"]`
silently produced 0.0 for every open Kalshi market once before).
`taker_fee_rate`/`maker_rebate_rate` are `market.fee.taker_rate`/
`market.fee.maker_rebate_rate` -- `app.venues.types.FeeSchedule`,
GUARDRAILS.md §1.5's one sanctioned source of a fee, never a literal.
`maker_rebate_rate` is carried here as data only: GUARDRAILS.md §2.3/D6
still forbid `FeeModel.fee()` from ever crediting it, and this migration
adds no code path that does.

All three columns are NULLABLE with NO backfill: every row this table
holds before this migration was collected before `collect_books` knew to
write them, and there is no venue call this migration could make (it
runs offline, GUARDRAILS.md §1/§2) to reconstruct a historical volume or
a historical fee schedule for a book already observed. A NULL here means
"collected before T8", not "zero" -- `mm_replay_snapshots` (T11) must
treat it as missing data, never as `0.0`.

Offline-safe (GUARDRAILS.md §2): three `ADD COLUMN` statements, no
`CREATE TYPE`, no reflection, nothing that needs a live connection --
`alembic upgrade head --sql` renders it whole, and this migration
executes NO `UPDATE`/`DELETE` (unlike `007`, which had rows to
reconcile) -- there is nothing to reconcile here, only new nullable
columns.

mm-proveout T15 (Phase 2 review, findings F1/F4/F5): added to THIS
migration, not a new `009`, because `008` has not been applied to any
database yet -- the only moment a column is free to add. Three findings
are irreversible the moment collection starts, and all three are missing
columns:

- `volume_lifetime` (Float, nullable): `volume` above is
  `venue_volume(market)`, which tries the 24-HOUR fields first
  (`volume_24h_fp`, `volume24hr`) because it is written for RANKING.
  Measured live 2026-09-07: Kalshi's `volume_24h_fp` did not move for a
  single one of 6,058 actively-traded markets over 245 seconds, while
  lifetime `volume_fp` moved for 25 of them -- and `passive_fill.py:142`
  returns no fills at all when `volume <= 0.0` (`:97` raises on a
  negative), gating BOTH fill models. `volume_lifetime` carries the
  LIFETIME counter instead (Kalshi `volume_fp`, Polymarket `volumeNum`),
  monotonicity-guarded: Gamma's lifetime `volumeNum` was also measured
  DECREASING for 29 of 255 markets over ~28 minutes, so a decrease is
  stored as `None` (unknown/restated), never the smaller value and never
  `0.0`.
- `observed_at` (DateTime(timezone=True), nullable): the poll's own
  wall-clock time, distinct from `ts` (the book's own identity). T8's
  same-`ts` refresh path means "the book is quiet" and "the collector
  died" are otherwise permanently indistinguishable, and the volume
  channel on a refreshed row is shifted forward by roughly one
  collection dwell -- `observed_at` is what lets a reader (T11) detect
  and bound that shift.
- `fee_source` (String(32), nullable): `market.fee.source` --
  `_published_fee_schedule` (Polymarket) falls back to
  `category_fee_schedule`, a table wrong on 82% of 1,918 live Polymarket
  markets, on a payload it cannot honour, and preflight cannot catch that
  fallback because a wrong default is a perfectly good float.
- `maker_fee_rate` (Float, nullable): `market.fee.maker_rate` -- only the
  taker rate was stored before this column, but D9's "fee in force" for
  a PASSIVE quoter is the MAKER rate (Kalshi `settings.kalshi_maker_fee_rate`
  = 0.0175; Polymarket 0.0), read through `market.fee`, never a literal.

All four are NULLABLE with NO backfill, same reasoning as the three
columns above: no venue call this migration could make, run offline, can
reconstruct a historical lifetime counter, poll time, fee source or
maker rate for a row already observed. This migration still executes NO
`UPDATE`/`DELETE` -- seven `ADD COLUMN` statements in total, nothing else.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic. Spelled with PEP 604 unions
# rather than `typing.Union`, matching `007` -- see that file's comment
# on why (`001`-`006`'s template predates it; this file is new).
revision: str = "008"
down_revision: str | None = "007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("book_snapshots", sa.Column("volume", sa.Float(), nullable=True))
    op.add_column(
        "book_snapshots", sa.Column("taker_fee_rate", sa.Float(), nullable=True)
    )
    op.add_column(
        "book_snapshots", sa.Column("maker_rebate_rate", sa.Float(), nullable=True)
    )
    op.add_column(
        "book_snapshots", sa.Column("volume_lifetime", sa.Float(), nullable=True)
    )
    op.add_column(
        "book_snapshots",
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "book_snapshots",
        sa.Column("fee_source", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "book_snapshots", sa.Column("maker_fee_rate", sa.Float(), nullable=True)
    )


def downgrade() -> None:
    """Irreversible: see the module docstring.

    All seven columns are the ONLY copy of a snapshot's volume and fee
    in force; no venue call can reconstruct a historical value for a
    book already observed (module docstring, "NULLABLE with NO
    backfill"). Dropping them here permanently discards whatever
    `collect_books` wrote, exactly as `007`'s downgrade documents for
    the row data it deletes -- matching that convention rather than
    leaving this one undocumented. Dropped in reverse of the order
    `upgrade()` adds them.
    """
    op.drop_column("book_snapshots", "maker_fee_rate")
    op.drop_column("book_snapshots", "fee_source")
    op.drop_column("book_snapshots", "observed_at")
    op.drop_column("book_snapshots", "volume_lifetime")
    op.drop_column("book_snapshots", "maker_rebate_rate")
    op.drop_column("book_snapshots", "taker_fee_rate")
    op.drop_column("book_snapshots", "volume")
