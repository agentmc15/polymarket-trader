"""The offline SQL for migration `008` adds columns and backfills nothing.

mm-proveout T8 (PLAN.md D9) brief: "adding them (nullable, no default
backfill)". `007` (the previous revision on `book_snapshots`) is the
counter-example this guards against -- it ran a `DELETE` and an `UPDATE`
to reconcile existing rows. `008` has nothing to reconcile: seven new
NULLABLE columns (three from T8, four more from mm-proveout T15's Phase
2 remediation -- added to this SAME migration because it has not been
applied to any database yet) with no historical data to backfill (there
is no venue call this migration could make to reconstruct a historical
volume, fee schedule, poll time, or fee source for a book already
observed -- see the migration's own docstring). This test renders the
ACTUAL offline SQL Alembic produces for just that one revision step --
via `alembic upgrade 007:008 --sql`, the same command family
GUARDRAILS.md §2 sanctions and the task's own verify command uses --
rather than re-reading the migration's Python source, so a rendering bug
(e.g. an accidental `server_default` that becomes a backfilling `UPDATE`
under the hood) would be caught here and not only by eyeballing the
`.py` file.

This never connects to a database: `--sql` is Alembic's offline mode
(`alembic/env.py::run_migrations_offline`), which configures a bare URL
and never builds an engine -- exactly the one migration command
GUARDRAILS.md permits a task to run.
"""
import subprocess
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]

#: Alembic emits one bookkeeping statement for EVERY revision step,
#: regardless of what that revision's own `upgrade()` does -- present in
#: `007`'s rendered SQL (which legitimately backfills) exactly as it is
#: here. Stripped before the "no UPDATE" check below, or that check would
#: fail on every revision ever written, including one with no backfill.
_VERSION_BOOKKEEPING = "UPDATE alembic_version SET version_num='008' WHERE alembic_version.version_num = '007';"


def _render_revision_008_sql() -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "007:008", "--sql"],
        cwd=_BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"alembic upgrade 007:008 --sql exited {result.returncode}:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result.stdout


#: T8's three columns plus T15's four -- all seven land in this one
#: migration (T15 was added to it, not a new `009`, because `008` has
#: not been applied to any database yet).
_ALL_SEVEN_COLUMNS = (
    "volume",
    "taker_fee_rate",
    "maker_rebate_rate",
    "volume_lifetime",
    "observed_at",
    "fee_source",
    "maker_fee_rate",
)


def test_revision_008_adds_all_seven_columns_and_backfills_nothing() -> None:
    sql = _render_revision_008_sql()

    for column in _ALL_SEVEN_COLUMNS:
        assert f"ADD COLUMN {column}" in sql, f"missing ADD COLUMN {column!r} in:\n{sql}"

    without_bookkeeping = sql.replace(_VERSION_BOOKKEEPING, "")
    assert "UPDATE" not in without_bookkeeping, (
        "migration 008 must add nullable columns with NO backfill -- an "
        "UPDATE statement (other than Alembic's own version bookkeeping) "
        "means a default was silently backfilled onto existing rows"
    )
    assert "DELETE" not in without_bookkeeping


def test_revision_008_columns_carry_no_not_null_and_no_default() -> None:
    """Adversarial: the "no UPDATE" check above catches a backfill done
    as a SEPARATE statement, but there is a second way a migration can
    silently give every existing row a value -- baking a `DEFAULT`
    straight onto the `ADD COLUMN` clause itself, which populates every
    existing row without ever emitting an `UPDATE`. `NOT NULL` is the
    other half of the brief's own words ("nullable, no default
    backfill"): a column added `NOT NULL` without a `DEFAULT` fails
    outright against a populated table, and one added `NOT NULL WITH
    DEFAULT` is exactly the silent-backfill case again -- so ruling out
    both `DEFAULT` and `NOT NULL` on each of the seven `ADD COLUMN`
    clauses is what actually proves 'nullable, no default backfill',
    not merely 'no separate UPDATE statement'.
    """
    sql = _render_revision_008_sql()
    for column in _ALL_SEVEN_COLUMNS:
        add_lines = [
            line for line in sql.splitlines() if f"ADD COLUMN {column} " in line
        ]
        assert add_lines, f"no ADD COLUMN statement found for {column!r} in:\n{sql}"
        for line in add_lines:
            assert "DEFAULT" not in line.upper(), line
            assert "NOT NULL" not in line.upper(), line
