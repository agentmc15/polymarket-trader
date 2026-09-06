"""Every `create_hypertable` must first drop the surrogate primary key.

TimescaleDB refuses to convert a table that carries ANY unique index
omitting the partitioning column. Both hypertables here have a
`PrimaryKeyConstraint("id")`, which omits it, so `create_hypertable`
failed with

    cannot create a unique index without the column "timestamp"
    (used in partitioning)

and took the whole chain down at migration 001. Nothing caught it
because GUARDRAILS.md sanctions only `alembic upgrade head --sql`, and
offline mode renders the DO block as text without executing it -- so
the SQL that could never run looked fine.

Verified against timescale/timescaledb:latest-pg15: with the DROP the
chain reaches 007 and both hypertables exist; without it, 001 aborts.

Row uniqueness does not depend on the dropped constraint. Each table
already has a natural unique key that DOES contain the partitioning
column, and that one is enforced on the hypertable's chunks -- observed
directly: a duplicate insert was rejected by
`_hyper_6_1_chunk_uq_price_history_market_timestamp`.

This is a source check rather than a live migration, so it holds with
no database and without running the command GUARDRAILS forbids.
"""
import re
from pathlib import Path

import pytest

_VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
_HYPERTABLE_RE = re.compile(r"create_hypertable\(\s*'([a-z_]+)'\s*,\s*'([a-z_]+)'")


def _migrations_with_hypertables() -> list[tuple[Path, str, str]]:
    found = []
    for path in sorted(_VERSIONS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for table, column in _HYPERTABLE_RE.findall(text):
            found.append((path, table, column))
    return found


def test_there_is_at_least_one_hypertable_to_check() -> None:
    """Guards the guard: an empty sweep must not read as success."""
    assert _migrations_with_hypertables(), "no create_hypertable calls found to check"


@pytest.mark.parametrize(
    ("path", "table", "column"),
    _migrations_with_hypertables(),
    ids=lambda v: v.name if isinstance(v, Path) else str(v),
)
def test_surrogate_pk_is_dropped_before_conversion(
    path: Path, table: str, column: str
) -> None:
    text = path.read_text(encoding="utf-8")
    drop = f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_pkey;"

    assert drop in text, (
        f"{path.name} converts '{table}' to a hypertable partitioned on "
        f"'{column}' without dropping {table}_pkey first. TimescaleDB will "
        f"reject the conversion, because that PK omits the partitioning column."
    )
    assert text.index(drop) < text.index(f"create_hypertable('{table}'"), (
        f"{path.name} drops {table}_pkey AFTER create_hypertable; the drop must "
        "come first or the conversion has already failed."
    )
