"""Opportunity-discovery API (PLAN.md D10, T19/T25).

`GET /opportunities`, `POST /scan` and `POST /scan/near-resolution` are
the human-facing surface of `app.services.scanner`'s two passes —
DISCOVERY ONLY. No route here places or routes an order: each `POST`
runs its pass synchronously and returns what it found; nothing here
calls `app.execution.router.OrderRouter.submit`, and that module is not
even imported below (see `app.services.scanner`'s module docstring for
the full rationale — routing an intent this API surfaces is a separate,
explicit call made elsewhere).

TWO PASSES, ONE LIST (T25). `scan()` covers the arbitrage strategies;
`near_resolution_pass()` covers `settlement_edge`, whose intents the
general pass CANNOT score at all (`app.services.scoring.score` refuses a
past-close market without `allow_past_close=True`, which only that pass
sets). They run on separate clocks and each mints its own `scan_id`, so
`GET /opportunities` keeps the newest `scan_id` PER PASS
(`extra_data[SCAN_PASS_KEY]`) instead of one global newest — otherwise
whichever pass ran last would silently erase the other's rows from the
list, and wiring the second pass up would have TAKEN AWAY a working
surface rather than adding one.

Every payload item carries `net_edge`, `annualized_return`,
`hours_to_resolution`, `fill_confidence`, `resolution_risk`,
`capital_lockup_usd`, `composite` (PLAN.md D10) PLUS `depth_source`,
`link_status` and `edge_basis` (GUARDRAILS.md §1.7 / PLAN.md D9): an
opportunity scored against `synthesize_book`'s invented depth, one built
on a `"proposed"` (unexecutable) event link, or one whose edge rests on
an ESTIMATED identity probability rather than on observed prices alone,
must say so in the payload itself — an operator reading this endpoint
has no other way to tell.
"""
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import AsyncSessionDep, MarketDataAdaptersDep
from app.config import settings
from app.models.event_link import EventLink
from app.models.intent import IntentRecord
from app.services.scanner import (
    ARBITRAGE_STRATEGIES,
    NEAR_RESOLUTION_STRATEGY,
    SCAN_PASS_KEY,
    ScoredIntent,
    near_resolution_pass,
    scan,
)
from app.strategies.base import Leg

router = APIRouter()


class OpportunityOut(BaseModel):
    """One scored, persisted opportunity — PLAN.md D10's payload contract.

    Every field named in T19's acceptance line is present and typed
    `float` (never `None`), plus `link_status`/`depth_source`/
    `edge_basis` (see the module docstring) and enough identity (`id`,
    `strategy`, `kind`, `legs`) for a caller to act on the row without a
    second lookup.

    Attributes:
        edge_basis: `app.services.scoring.OpportunityScore.edge_basis`,
            promoted out of the generic `metadata` blob to a column of
            its own (T33). `"observed_costs"` means every term of
            `net_edge` is an observed price or a published fee/gas rate;
            `"identity_estimated"` means it also carries a haircut
            driven by the MATCHER'S SIMILARITY SCORE, which is not a
            calibrated probability. Two rows with the same `composite`
            are the same expected return only to the extent that
            estimate is calibrated, so a consumer sorting by `composite`
            needs this beside the sort key, not buried in a dict.
            `None` — never one of the two labels by default — when the
            persisted row carries no basis at all (written before
            `edge_basis` existed). Guessing `"observed_costs"` there
            would make an unlabeled row indistinguishable from a
            genuinely arithmetic one, which is the exact failure mode
            GUARDRAILS.md §1.7 exists to prevent.
    """

    id: str
    strategy: str
    kind: str
    status: str
    mode: str
    created_at: datetime | None
    legs: list[dict[str, Any]]
    net_edge: float
    annualized_return: float
    hours_to_resolution: float
    fill_confidence: float
    resolution_risk: float
    capital_lockup_usd: float
    composite: float
    link_status: str | None
    depth_source: str
    edge_basis: str | None
    metadata: dict[str, Any]


class OpportunitiesResponse(BaseModel):
    """Response for `GET /opportunities`."""

    opportunities: list[OpportunityOut]
    count: int


class ScanResponse(BaseModel):
    """Response for `POST /scan`."""

    status: str
    mode: str
    opportunities_found: int
    opportunities: list[OpportunityOut]


class ExecutedIntentOut(BaseModel):
    """One executed intent, for `GET /history`."""

    id: str
    strategy: str
    kind: str
    status: str
    mode: str
    created_at: datetime | None
    legs: list[dict[str, Any]]
    score: dict[str, Any]


class HistoryResponse(BaseModel):
    """Response for `GET /history`."""

    trades: list[ExecutedIntentOut]
    total: int


def _latest_scan_rows(rows: list[IntentRecord]) -> list[IntentRecord]:
    """Keep only the newest scan's rows, PER SCANNER PASS.

    TWO RULES, EACH FIXING A WAY THE OLD ONE-GLOBAL-`scan_id` FILTER LIED.

    1. A row with NO `scan_id` is DROPPED, never listed and never allowed
       to define "latest". Such a row did not come from a scanner pass at
       all: `app.execution.router.OrderRouter._persist_pending` writes a
       `pending` `IntentRecord` at ROUTING time with no `scan_id` in
       `extra_data`, and if one of those happened to be the newest row
       the old code read `latest_scan_id` as `None` and then skipped the
       filter ENTIRELY — returning every pending scored intent from every
       historical scan, presented as the current list. A single untagged
       row silently turned "the latest scan" into "everything, ever".
    2. Among the rows that DO carry one, the newest `scan_id` is taken
       per `extra_data[SCAN_PASS_KEY]`. `scan()` and
       `near_resolution_pass()` run on separate clocks and mint separate
       ids, so a single global newest would show only whichever pass ran
       most recently and hide the other's rows completely (see the module
       docstring). A row written before that key existed groups under
       `None` and keeps exactly its old, single-group behaviour.

    Args:
        rows: Scored `pending` rows for the current mode, ALREADY sorted
            newest-created first (this function reads that order to
            decide which `scan_id` is newest in each group).

    Returns:
        list[IntentRecord]: The subset belonging to the newest scan of
            each pass, in the order given.
    """
    scan_rows = [
        row for row in rows if (row.extra_data or {}).get("scan_id") is not None
    ]
    latest_by_pass: dict[str | None, str] = {}
    for row in scan_rows:
        extra = row.extra_data or {}
        pass_name = extra.get(SCAN_PASS_KEY)
        if pass_name not in latest_by_pass:
            latest_by_pass[pass_name] = extra["scan_id"]
    return [
        row
        for row in scan_rows
        if (row.extra_data or {})["scan_id"]
        == latest_by_pass.get((row.extra_data or {}).get(SCAN_PASS_KEY))
    ]


def _leg_out(leg: Leg) -> dict[str, Any]:
    """Return one `Leg` as a JSON-serializable dict, field by field."""
    return {
        "venue": leg.venue,
        "market_id": leg.market_id,
        "outcome": leg.outcome,
        "side": leg.side,
        "limit_price": leg.limit_price,
        "size_contracts": leg.size_contracts,
        "size_usd": leg.size_usd,
    }


def _opportunity_out_from_record(row: IntentRecord) -> OpportunityOut:
    """Build an `OpportunityOut` field by field from a persisted `IntentRecord`.

    Args:
        row: A `pending` row whose `score` is non-empty (callers filter
            that before calling this).

    Returns:
        OpportunityOut: The row, rendered.
    """
    score = row.score or {}
    return OpportunityOut(
        id=row.id,
        strategy=row.strategy,
        kind=row.kind,
        status=row.status,
        mode=row.mode,
        created_at=row.created_at,
        legs=list(row.legs or []),
        net_edge=float(score.get("net_edge", 0.0)),
        annualized_return=float(score.get("annualized_return", 0.0)),
        hours_to_resolution=float(score.get("hours_to_resolution", 0.0)),
        fill_confidence=float(score.get("fill_confidence", 0.0)),
        resolution_risk=float(score.get("resolution_risk", 0.0)),
        capital_lockup_usd=float(score.get("capital_lockup_usd", 0.0)),
        composite=float(score.get("composite", 0.0)),
        link_status=score.get("link_status"),
        depth_source=str(score.get("depth_source", "synthetic")),
        # NO DEFAULT LABEL. `depth_source` above can fall back to
        # `"synthetic"` because that is the CONSERVATIVE reading of an
        # absent value. `edge_basis` has no conservative default —
        # `"observed_costs"` would understate the row's model risk and
        # `"identity_estimated"` would invent a haircut that was never
        # applied — so an unlabeled row reports `None` and says nothing.
        edge_basis=(
            str(score["edge_basis"]) if score.get("edge_basis") is not None else None
        ),
        metadata=dict(row.extra_data or {}),
    )


def _opportunity_out_from_scored(item: ScoredIntent) -> OpportunityOut:
    """Build an `OpportunityOut` directly from a fresh `scan()` result.

    Used by `POST /scan`, which has the in-memory `ScoredIntent`s from
    the pass it just ran and has no need to re-query what it just wrote.

    Args:
        item: One scored intent from `app.services.scanner.scan()`.

    Returns:
        OpportunityOut: The scored intent, rendered.
    """
    opportunity_score = item.score
    return OpportunityOut(
        id=item.intent_record_id,
        strategy=item.strategy,
        kind=item.intent.kind,
        status="pending",
        mode=settings.trading_mode,
        created_at=None,
        legs=[_leg_out(leg) for leg in item.intent.legs],
        net_edge=opportunity_score.net_edge,
        annualized_return=opportunity_score.annualized_return,
        hours_to_resolution=opportunity_score.hours_to_resolution,
        fill_confidence=opportunity_score.fill_confidence,
        resolution_risk=opportunity_score.resolution_risk,
        capital_lockup_usd=opportunity_score.capital_lockup_usd,
        composite=opportunity_score.composite,
        link_status=opportunity_score.link_status,
        depth_source=opportunity_score.depth_source,
        edge_basis=opportunity_score.edge_basis,
        metadata=dict(item.intent.metadata),
    )


@router.get("/opportunities")
async def list_opportunities(
    session: AsyncSessionDep,
    min_composite: float | None = Query(
        default=None, description="Inclusive floor on OpportunityScore.composite."
    ),
    venue: str | None = Query(
        default=None, description="Restrict to opportunities with a leg on this venue."
    ),
    near_resolution: bool | None = Query(
        default=None,
        description=(
            "True: only hours_to_resolution <= settings.near_resolution_hours; "
            "False: only beyond it; omitted: no filter."
        ),
    ),
) -> OpportunitiesResponse:
    """Return the latest scan's scored, pending opportunities.

    "Latest scan" means every `pending`, scored `IntentRecord` sharing
    the newest `scan_id` OF ITS OWN SCANNER PASS, in the CURRENTLY
    configured `settings.trading_mode` (PLAN.md D4: every reader of
    `intents` filters on `mode`) — a row from an old scan, from the
    other trading mode, or from the order router rather than a scanner
    is never mixed into the current list. See `_latest_scan_rows` for
    why the grouping is per pass and why an untagged row is excluded.

    So an operator filtering `near_resolution=true` sees the newest
    near-resolution pass's rows whether or not an arbitrage scan has run
    since, and vice versa.

    Args:
        session: Database session.
        min_composite: Inclusive floor on `composite`.
        venue: Restrict to opportunities with a leg on this venue.
        near_resolution: Restrict by `hours_to_resolution` vs
            `settings.near_resolution_hours`.

    Returns:
        OpportunitiesResponse: Matching opportunities, sorted by
            `composite` descending.
    """
    query = (
        select(IntentRecord)
        .where(IntentRecord.status == "pending")
        .where(IntentRecord.mode == settings.trading_mode)
        .order_by(IntentRecord.created_at.desc())
    )
    rows = [row for row in (await session.execute(query)).scalars().all() if row.score]
    if not rows:
        return OpportunitiesResponse(opportunities=[], count=0)

    outputs = [_opportunity_out_from_record(row) for row in _latest_scan_rows(rows)]

    if min_composite is not None:
        outputs = [o for o in outputs if o.composite >= min_composite]
    if venue is not None:
        outputs = [o for o in outputs if any(leg.get("venue") == venue for leg in o.legs)]
    if near_resolution is not None:
        threshold = settings.near_resolution_hours
        outputs = [
            o
            for o in outputs
            if (o.hours_to_resolution <= threshold) == near_resolution
        ]

    outputs.sort(key=lambda opportunity: opportunity.composite, reverse=True)
    return OpportunitiesResponse(opportunities=outputs, count=len(outputs))


@router.post("/scan")
async def trigger_scan(
    session: AsyncSessionDep,
    adapters: MarketDataAdaptersDep,
    strategies: list[str] | None = Query(
        default=None,
        description="Strategy registry keys to run; defaults to the arbitrage category.",
    ),
) -> ScanResponse:
    """Run one discovery scan now and return what it found.

    Always runs `app.services.scanner.scan()` in-process and awaits it —
    the SAME code path in a test and in production, so this is
    "synchronous" in both. The separate, already-scheduled Celery beat
    (`app.tasks.scanner.scan_opportunities`, every
    `settings.scan_interval_s`) is what "enqueues" the periodic passes in
    production; this route is the on-demand trigger with an immediate
    result, not a fork on environment.

    `adapters` come from `app.api.deps.get_market_data_adapters`
    (`app.venues.registry.get_read_adapter`, never `get_adapter`) — this
    route cannot acquire an order-placing adapter, and nothing it calls
    routes an order (see `app.services.scanner`'s module docstring).

    Args:
        session: Database session.
        adapters: Read-only market-data adapters, one per venue.
        strategies: Strategy registry keys to run. Defaults to
            `app.services.scanner.ARBITRAGE_STRATEGIES`.

    Returns:
        ScanResponse: What this pass found, sorted by `composite`
            descending.

    Raises:
        HTTPException: 400 if `strategies` names
            `app.services.scanner.NEAR_RESOLUTION_STRATEGY`, which this
            pass structurally cannot score — see below.
    """
    links = list((await session.execute(select(EventLink))).scalars().all())
    names = strategies if strategies else list(ARBITRAGE_STRATEGIES)
    if NEAR_RESOLUTION_STRATEGY in names:
        # NOT a taste call about where a strategy "belongs". `scan()`
        # calls `score(intent, ctx)` WITHOUT `allow_past_close=True`, and
        # every intent this strategy emits is on a market whose
        # `close_time` has already passed (that is what "outcome
        # determined" means), so `UnscorableIntent` is raised for 100% of
        # them and every one is silently skipped. Answering `200 {"found":
        # 0}` would report "no settlement edges" when what actually
        # happened is that this pass cannot see any. Sending the caller to
        # the pass that CAN is the only truthful response.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"'{NEAR_RESOLUTION_STRATEGY}' cannot be scored by the general "
                "scan (its markets are past close). Use "
                "POST /arbitrage/scan/near-resolution instead."
            ),
        )
    scored = await scan(names, adapters, links, session)
    return ScanResponse(
        status="completed",
        mode=settings.trading_mode,
        opportunities_found=len(scored),
        opportunities=[_opportunity_out_from_scored(item) for item in scored],
    )


@router.post("/scan/near-resolution")
async def trigger_near_resolution_scan(
    session: AsyncSessionDep,
    adapters: MarketDataAdaptersDep,
) -> ScanResponse:
    """Run one near-resolution pass now and return what it found (T25).

    The on-demand twin of the `scan-near-resolution` Celery beat
    (`app.tasks.scanner.scan_near_resolution`, every
    `settings.near_resolution_scan_interval_s`), exactly as `POST /scan`
    is the on-demand twin of `scan_opportunities`: same in-process
    `await`, same code path in a test and in production.

    It takes no `strategies` parameter. `near_resolution_pass()` runs
    `app.strategies.settlement_edge.SettlementEdgeStrategy` and nothing
    else, by design — the pass's `allow_past_close=True` scoring and its
    `list_markets(status=None)` selection are correct for that regime and
    for no other (see `app.services.scanner`'s module docstring), so
    there is no set of names to choose from.

    `strategy_config` is left at the strategy's shipped `DEFAULT_CONFIG`
    and is deliberately NOT exposed as a query parameter: it carries
    `allow_dispute_window`, and an HTTP caller must not be able to switch
    a risk gate off from a URL.

    Placement-wise this route is identical to `POST /scan`: read-only
    adapters from `app.api.deps.get_market_data_adapters`, and nothing it
    calls routes an order.

    Args:
        session: Database session.
        adapters: Read-only market-data adapters, one per venue.

    Returns:
        ScanResponse: What this pass found, sorted by `composite`
            descending. Every item's `metadata["bucket"]` is
            `"near_resolution"`.
    """
    scored = await near_resolution_pass(adapters, session)
    return ScanResponse(
        status="completed",
        mode=settings.trading_mode,
        opportunities_found=len(scored),
        opportunities=[_opportunity_out_from_scored(item) for item in scored],
    )


@router.get("/history")
async def arbitrage_history(
    session: AsyncSessionDep,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> HistoryResponse:
    """List executed intents, most recent first.

    Args:
        session: Database session.
        skip: Number of records to skip.
        limit: Maximum number of records to return.

    Returns:
        HistoryResponse: Executed intents plus the total executed count.
    """
    query = (
        select(IntentRecord)
        .where(IntentRecord.status == "executed")
        .order_by(IntentRecord.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    rows = (await session.execute(query)).scalars().all()
    total = (
        await session.execute(
            select(func.count()).select_from(IntentRecord).where(
                IntentRecord.status == "executed"
            )
        )
    ).scalar_one()

    return HistoryResponse(
        trades=[
            ExecutedIntentOut(
                id=row.id,
                strategy=row.strategy,
                kind=row.kind,
                status=row.status,
                mode=row.mode,
                created_at=row.created_at,
                legs=list(row.legs or []),
                score=dict(row.score or {}),
            )
            for row in rows
        ],
        total=total,
    )
