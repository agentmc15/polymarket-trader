"""Base model with common fields."""
from datetime import datetime

from sqlalchemy import JSON, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: Dict-shaped JSON column: plain JSON on SQLite (tests), JSONB on Postgres.
#:
#: `none_as_null=True` on both variants makes a Python `None` value store as a
#: real SQL NULL (instead of the JSON literal `'null'`), so a `NOT NULL`
#: constraint on the column can actually reject it. `MutableDict.as_mutable`
#: wraps the type so in-place mutation of the loaded dict (`row.extra_data["k"]
#: = v`) is tracked and flushed like a normal attribute assignment would be.
JSONDict = MutableDict.as_mutable(
    JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")
)
#: List-shaped JSON column: plain JSON on SQLite (tests), JSONB on Postgres.
#: See `JSONDict` above for the `none_as_null` and mutable-tracking rationale.
JSONList = MutableList.as_mutable(
    JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")
)


class Base(DeclarativeBase):
    """Base class for all models.

    No custom `__init__` is defined here. SQLAlchemy auto-injects its
    standard `sqlalchemy.orm.decl_base._declarative_constructor` on any
    declarative class that doesn't define its own `__init__`: it accepts
    only kwargs naming a mapped attribute (raising `TypeError` otherwise)
    and does nothing else — in particular it does NOT apply any column's
    `default=`.

    Consequence for every mapped column, scalar or JSON alike:
    `mapped_column(default=...)` is applied by SQLAlchemy at INSERT-compile
    time (on flush), never at `__init__`. A freshly constructed, un-flushed
    instance therefore reads back `None` for any column whose value wasn't
    passed as a kwarg, even when the column is non-nullable and typed as a
    non-Optional `Mapped[...]` — e.g. (measured against this codebase's own
    models) `Market(condition_id="x", question="q").extra_data is None`
    (flush -> `{}`), `.is_active is None` (flush -> `True`), and
    `Order(...).status is None` / `.order_type is None` (flush ->
    `OrderStatus.PENDING` / `OrderType.GTC`). In-place mutation of an
    unflushed JSON column (`instance.extra_data["k"] = 1`) raises
    `TypeError` before the first flush, for the same reason. Construct with
    explicit values for any column you intend to read or mutate before the
    first flush.

    This is deliberate, not an oversight. An earlier revision of this class
    defined a custom `__init__` that eagerly computed each unset column's
    Python-side default (so `Market(condition_id="x",
    question="q").extra_data` was `{}` immediately), matching what a flush
    would produce for JSON/list-shaped defaults. That was reverted because
    it silently corrupts data through `session.merge()`: three mechanisms
    for applying that eager default were tried (plain `setattr`, writing
    straight into `self.__dict__` to bypass the instrumented descriptor,
    and `sqlalchemy.orm.attributes.set_committed_value`), and all three
    still leave the attribute's key present in the instance's `__dict__`.
    `session.merge()` decides whether to overwrite a persisted attribute
    purely by checking whether that key is present in the *source*
    object's `__dict__` (`sqlalchemy.orm.properties.ColumnProperty.merge`
    checks `self.key in source_dict`) — it has no visibility into *how* or
    *why* the key got there. So `session.merge(Market(id=existing_id,
    condition_id="c9", question="updated"))` — a plausible upsert pattern,
    not passing `outcomes`/`extra_data` because the caller only means to
    update `question` — would silently overwrite the persisted
    `outcomes`/`extra_data` with fresh empty defaults. No supported
    SQLAlchemy hook (the `init`/`load` instance events; there is no
    "before merge" session event) runs between construction and
    `ColumnProperty.merge()`'s per-attribute walk, so there is no way to
    eagerly default a column *and* keep its key out of `__dict__` for
    merge's sake. A loud `TypeError` on premature mutation is preferable to
    a silently corrupted row, so this class does not paper over
    SQLAlchemy's construction-vs-flush timing gap.
    """

    type_annotation_map = {
        datetime: DateTime(timezone=True),
    }


class TimestampMixin:
    """Mixin for created_at and updated_at timestamps."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
