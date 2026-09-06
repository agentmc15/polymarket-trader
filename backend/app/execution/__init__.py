"""Execution: the one path from an `Intent` to venue orders (PLAN.md D4/D5).

`SimulatedFillEngine` is shared by the backtester (`app/services/
backtesting/engine.py`, T08) and the paper adapter (`PaperVenueAdapter`,
`app/venues/paper.py`) — one fill model, one fee model, one set of
rounding rules, so paper and backtest can never disagree about what an
order would have got. `CapitalLedger` (T14) is the per-venue capital the
router spends from, and is the SHARED abstraction the backtester's
portfolio is meant to grow into: it is pure in-memory, takes no session
and does no I/O, precisely so it can live inside a replay loop that has
no database.

`OrderRouter` and `reconcile` are deliberately NOT re-exported here.
They import `app.models` and `app.venues`, while `app.venues.paper`
imports back into this package for the fill engine; keeping this
`__init__` to the leaf modules (`fill_engine`, `ledger`) keeps that
graph acyclic no matter which module a caller imports first. Import them
from their own modules — `from app.execution.router import OrderRouter`,
`from app.execution.reconcile import reconcile` — exactly as
`app.execution.fences` is already imported everywhere.

GUARDRAILS.md §1.1: nothing in this package places a real order. It
simulates fills against an `OrderBook`; the router calls `place_order`
on whatever adapter it was handed, and only the two `live.py` modules
may hold a venue's real order-placement call.
"""
from app.execution.fill_engine import (
    CROSSED_QUOTES_KEY,
    FEE_SOURCE_KEY,
    TICK_VALIDATED_KEY,
    FillReason,
    FillResult,
    FillStatus,
    SimulatedFillEngine,
    synthesize_book,
)
from app.execution.ledger import (
    CapitalLedger,
    InsufficientCapital,
    LedgerError,
    Reservation,
    ReservationId,
    UnknownReservation,
    UnknownVenue,
    VenueCapital,
)

__all__ = [
    "CROSSED_QUOTES_KEY",
    "FEE_SOURCE_KEY",
    "TICK_VALIDATED_KEY",
    "CapitalLedger",
    "FillReason",
    "FillResult",
    "FillStatus",
    "InsufficientCapital",
    "LedgerError",
    "Reservation",
    "ReservationId",
    "SimulatedFillEngine",
    "UnknownReservation",
    "UnknownVenue",
    "VenueCapital",
    "synthesize_book",
]
