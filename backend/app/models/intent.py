"""Persisted record of an `app.strategies.base.Intent` (T15, PLAN.md D7).

`IntentRecord` is the durable audit trail for every multi-leg execution
unit a strategy emits: `OrderRouter` (T14) writes one row per `Intent` it
receives, before placing any leg, so a crash mid-submission still leaves
a record of what was ATTEMPTED (status starts `"pending"`) even if no
`Order`/`Trade` row ever gets an ack. `legs`/`score` are stored as JSON
rather than normalized tables because nothing queries into their
internals — they are read back whole, as a record of what was decided
and why, the same rationale `BacktestRun.report` uses (migration `003`).

MASS-ASSIGNMENT WARNING (Phase 0 security audit; carried forward from
T13/T15 into every later task that writes to this table or to
`Order`/`Trade`/`Position`, all of which now carry a `mode` column too):

SQLAlchemy's stock `_declarative_constructor` (see `app.models.base.Base`'s
docstring) accepts ANY keyword that names a mapped column and does
nothing else — it will happily set `id`, `mode`, `status`, or
`client_order_id` from an arbitrary dict, including one built from an
HTTP request body. `mode` (`"paper"` vs `"live"`) is the single column
that decides whether a row represents a SIMULATED order or REAL MONEY.
A caller who can set `mode` — directly, or indirectly through
`IntentRecord(**request.model_dump())` or any other splat of
caller-controlled data — can mislabel a live order as paper (hiding real
risk from monitoring that filters on `mode="live"`) or a paper order as
live (corrupting real P&L accounting with fake fills).

The binding rule for T14 (`OrderRouter`, which first writes these rows)
and T16 (the API routes that reach it): construct `IntentRecord` /
`Order` / `Trade` / `Position` FIELD BY FIELD from a validated pydantic
request/response schema. Never `Model(**request.model_dump())`, never
`Model(**dict_from_untrusted_source)`. `mode` in particular should come
from server-side configuration/routing logic (`settings.trading_mode`,
the registry's active mode) — NOT from a client-supplied field — unless
a future design explicitly needs a caller to choose per-request, in
which case that field must be validated against a closed enum AND
authorized (e.g. a live order might require the same
`LIVE_TRADING_CONFIRMATION` discipline `app.execution.fences` already
enforces for adapter construction) before it ever reaches model
construction.

The `CheckConstraint`s on `kind`/`mode`/`status` below are defense in
depth (a malformed value still can't be committed), not a substitute for
validating before construction — they run at flush/commit, by which
point a mislabeled row may already have driven a decision in the calling
code.
"""
from datetime import datetime
from typing import Literal

from sqlalchemy import CheckConstraint, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, JSONDict, JSONList

#: Mirrors `app.strategies.base.IntentKind` exactly (not imported — see
#: `app.models.trade._VENUE_VALUES` for why `app.models` does not import
#: from sibling packages at runtime; keep these in sync by hand).
IntentRecordKind = Literal["single", "complement", "bundle", "cross_venue"]

#: Mirrors `app.venues.registry.TradingMode` exactly. See
#: `app.models.trade._MODE_VALUES` — `mode` is the paper/live separator
#: here too, and every reader of `intents` must filter on it for the same
#: reason (PLAN.md D4).
IntentRecordMode = Literal["paper", "live"]

#: Lifecycle of a persisted intent. `"pending"` = written before any leg
#: was placed (T14 step 3: persist BEFORE calling the venue, for
#: crash-safety); `"executed"` = at least one leg filled per the
#: intent's `atomicity` policy; `"rejected"` = failed a pre-flight check
#: (risk limits, capital reservation) before any leg was placed;
#: `"expired"` = a resting/pending intent that timed out without
#: resolving to either of the above.
IntentRecordStatus = Literal["pending", "executed", "rejected", "expired"]

_KIND_VALUES = ("single", "complement", "bundle", "cross_venue")
_MODE_VALUES = ("paper", "live")
_STATUS_VALUES = ("pending", "executed", "rejected", "expired")


class IntentRecord(Base):
    """Durable record of one `app.strategies.base.Intent` submission.

    Not a `TimestampMixin` user: this table intentionally has only
    `created_at` (when the intent was first recorded), not an
    `updated_at` — `status` transitions are audit-worthy events that
    belong in `extra_data`/logging if a full history is ever needed,
    rather than overwriting a single "last modified" timestamp.

    Attributes:
        id: Primary key, assigned by the caller (T14) — NOT
            autoincrement. Must be a short, collision-resistant string;
            `OrderRouter` mints `Order.client_order_id` as
            `f"{id}:{leg_index}:{attempt}"`, and that column is
            `String(64)`. A UUID4 string (`str(uuid.uuid4())`, 36 chars)
            leaves 36 - 64 = 28 characters of headroom for
            `:{leg_index}:{attempt}`, more than enough for any realistic
            leg count (`Intent.kind="bundle"` legs are a handful, not
            hundreds) and retry attempt count (a bounded small number,
            not unbounded retries). A future id scheme must not silently
            exceed this budget — either keep ids at or under ~36
            characters, or widen this column AND `Order.client_order_id`/
            `Order.intent_id`/`Position.intent_id` (all `String(64)`) in
            the SAME follow-up migration.
        kind: See `IntentRecordKind`.
        strategy: The strategy name/id that produced this intent (matches
            `app.models.strategy.Strategy.name`'s `String(100)` sizing,
            though there is no FK — a strategy need not be persisted to
            emit an intent, e.g. a strategy under active development).
        mode: See `IntentRecordMode` and this module's mass-assignment
            warning above. Indexed: every reader must filter on it.
        created_at: When this row was first written (server-side
            default, aware UTC).
        status: See `IntentRecordStatus`. Indexed for "list pending
            intents" / reconciliation queries. Defaults `"pending"`.
        legs: JSON list mirroring `Intent.legs` (each leg's venue,
            market_id, outcome, side, price, size) as submitted — a
            snapshot, not a live reference, so it stays meaningful even
            if the strategy or market data changes later.
        score: JSON dict mirroring `app.services.scoring.OpportunityScore`
            (net_edge, annualized_return, hours_to_resolution,
            fill_confidence, resolution_risk, capital_lockup_usd,
            composite) at the time this intent was decided, or `{}` if
            unscored.
        extra_data: Free-form metadata (mirrors `Intent.metadata`).
    """

    __tablename__ = "intents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    strategy: Mapped[str] = mapped_column(String(100), nullable=False)
    mode: Mapped[str] = mapped_column(String(8), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending", index=True, nullable=False
    )
    legs: Mapped[list] = mapped_column(JSONList, default=list)
    score: Mapped[dict] = mapped_column(JSONDict, default=dict)
    extra_data: Mapped[dict] = mapped_column("extra_data", JSONDict, default=dict)

    __table_args__ = (
        Index("ix_intents_created_at", "created_at"),
        CheckConstraint(
            f"kind IN ({', '.join(repr(v) for v in _KIND_VALUES)})",
            name="ck_intents_kind_valid",
        ),
        CheckConstraint(
            f"mode IN ({', '.join(repr(v) for v in _MODE_VALUES)})",
            name="ck_intents_mode_valid",
        ),
        CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in _STATUS_VALUES)})",
            name="ck_intents_status_valid",
        ),
    )
