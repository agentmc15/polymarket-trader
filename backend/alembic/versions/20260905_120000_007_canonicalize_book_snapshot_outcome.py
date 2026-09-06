"""Canonicalize book_snapshots.outcome to its identity form.

Revision ID: 007
Revises: 006
Create Date: 2026-09-05 12:00:00.000000+00:00

T21d defect 6 (NOTES.md). `app.models.book_snapshot.BookSnapshot`'s
docstring has always promised that a lookup by
`(venue, market_id, outcome)` "can never miss a row purely because of a
casing mismatch between two venues' payloads", but
`DataCollector._upsert_book_snapshot` implemented it with
`app.strategies.base.normalize_outcome()` — which canonicalizes
`"YES"`/`"NO"` and passes every other label through verbatim. The
guarantee therefore held for exactly the binary case that never needed
it, while the arbitrary, non-binary outcome labels T21 introduced (a
named candidate in a multi-outcome market) were stored under whatever
casing and whitespace the venue payload happened to carry. Four
collections of the SAME book at the SAME `ts`, spelled `'Trump'`,
`'TRUMP'`, `'trump'` and `'Trump '`, wrote FOUR rows straight past the
`uq_book_snapshots_venue_market_outcome_ts` constraint that exists to
make that impossible, and `DataReplayer._get_recorded_book` — which
matches `outcome` exactly — found none of them for a caller asking under
a fifth spelling. A non-binary leg with no recorded book cannot fill at
all (`Backtester._book_for` refuses to synthesize depth for a label it
has never observed), so this was silent data loss, not merely
duplication.

The collector now writes `app.strategies.base.outcome_key()`: `'YES'` /
`'NO'` for the binary pair, and every other label stripped of
surrounding whitespace and lower-cased. This migration brings rows
written under the old rule onto the same rule, in two steps that MUST
run in this order:

1. DELETE the rows that would collide once canonicalized, keeping the
   lowest `id` in each collision group. Those rows are duplicates by
   construction — same venue, same market, same outcome, same observed
   instant — so which one survives is immaterial; the constraint simply
   has to be satisfiable before step 2 can run.
2. UPDATE the survivors to the canonical form.

No `'yes'`/`'no'` row can be affected by step 1 for a reason worth
stating: those were ALREADY canonicalized on the way in, so every
binary row is already exactly `'YES'` or `'NO'` and step 2's `WHERE`
clause skips it. Only non-binary labels move.

Offline-safe (GUARDRAILS.md §2): two plain DML statements, no
`CREATE TYPE`, no reflection, nothing that needs a live connection —
`alembic upgrade head --sql` renders it whole. Unlike `001`/`006` there
is no `DO $$ ... $$` extension guard here because there is nothing to
guard: this migration calls no TimescaleDB function. `book_snapshots` IS
a hypertable when the extension is installed, and `UPDATE`/`DELETE`
against one behave exactly as against a plain table — `006` enables no
compression policy, and it is only compressed chunks that refuse DML.

`lower()` here stands in for Python's `str.casefold()`, and the two can
disagree for a handful of non-ASCII forms (German 'ß', Turkish
dotless 'i'). Every label this table has actually collected is an
ASCII venue outcome name, where they are identical. Should a row ever
survive with an identity Python would spell differently, the failure is
now the LOUD one rather than the silent one this task removed: the
book is not found, the non-binary leg does not fill, and any position
that cannot be marked is reported on
`BacktestResult.unmarked_positions`.

DOWNGRADE IS A NO-OP, deliberately. The original casing of a canonicalized
label is not recoverable, and the duplicate rows step 1 removes are gone.
Re-splitting `'trump'` back into four spellings is not a thing a
downgrade could do even in principle, and inventing one spelling would
put a label in the table that no venue ever sent.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic. Spelled with PEP 604 unions
# rather than `typing.Union` as `001`-`006` do: the alembic template
# those were generated from predates it, and this file is new, so it is
# written to the repo's ruff config (`UP007`) instead of inheriting a
# finding from the template.
revision: str = "007"
down_revision: str | None = "006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The SQL spelling of `app.strategies.base.outcome_key()`. Written out
#: once and reused by both statements below so the dedupe grouping and
#: the rewrite can never drift apart — if they disagreed, step 1 would
#: leave exactly the collisions step 2 then fails on.
_CANONICAL_OUTCOME = """
        CASE
            WHEN lower(btrim(outcome)) IN ('yes', 'no')
                THEN upper(btrim(outcome))
            ELSE lower(btrim(outcome))
        END
"""


def upgrade() -> None:
    op.execute(
        f"""
        DELETE FROM book_snapshots
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM book_snapshots
            GROUP BY venue, market_id, ts, {_CANONICAL_OUTCOME}
        );
        """
    )
    op.execute(
        f"""
        UPDATE book_snapshots
        SET outcome = {_CANONICAL_OUTCOME}
        WHERE outcome <> {_CANONICAL_OUTCOME};
        """
    )


def downgrade() -> None:
    """Irreversible: see the module docstring."""
