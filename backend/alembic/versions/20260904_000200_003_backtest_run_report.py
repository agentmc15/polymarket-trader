"""Add backtest_runs.report for the trustworthiness/coverage payload.

Revision ID: 003
Revises: 002
Create Date: 2026-09-04 00:02:00.000000+00:00

Rationale (PLAN.md D6, GUARDRAILS.md §1.7): a backtest number is only
usable alongside the facts that qualify it — whether the depth it filled
against was recorded or synthesized, whether fills were taken from the
signal's own snapshot (look-ahead) or the next one, how many of the
markets it traded actually RESOLVED versus were marked to a last quote,
how much of the final value is still unrealized, and how many intents
were rejected or expired rather than executed. Before this column those
facts existed only inside the in-process `BacktestResult` and were
discarded the moment the Celery task returned, so the API could serve a
return figure with no way to say how much to trust it.

Rather than add a dozen scalar columns (each of which would need another
migration as the report grows), the whole payload lands in one
JSON/JSONB blob. It is a REPORT, not a queried dimension: nothing filters
or aggregates on it, so there is no index and no need for typed columns.

`server_default="{}"` (an empty JSON object, not NULL) means existing
rows come back as `{}` rather than `None`, so every reader can treat the
column as a dict unconditionally — and an OLD row's empty report is
correctly read as "this run predates coverage reporting", never as
"coverage was zero".

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Mirrors `app.models.base.JSONDict`'s underlying type: plain JSON on
#: SQLite (tests), JSONB on Postgres. Declared here rather than imported
#: so the migration stays valid if the model module is later refactored —
#: a migration describes the schema as it was, not as the models are now.
_JSON_TYPE = sa.JSON(none_as_null=True).with_variant(
    postgresql.JSONB(none_as_null=True), "postgresql"
)


def upgrade() -> None:
    op.add_column(
        "backtest_runs",
        sa.Column(
            "report",
            _JSON_TYPE,
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("backtest_runs", "report")
