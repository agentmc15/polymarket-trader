"""Per-venue capital ledger (PLAN.md R6/D8, GUARDRAILS.md §1.6, T14).

CAPITAL IS PER VENUE, AND THAT IS NOT A STYLISTIC CHOICE. Kalshi dollars
sit in a CFTC-regulated FCM account and move by ACH/wire on a timescale
of DAYS (PLAN.md §3; `Settings.transfer_latency_hours` defaults to 72),
while Polymarket dollars are USDC on Polygon. Inside the lifetime of a
trade the two pools are disjoint. A cross-venue intent is therefore
bounded by `min(available_polymarket, available_kalshi)` — see
`min_available()` — and NEVER by any total across venues. This module
consequently has no method that adds one venue's free balance to
another's, and T14's acceptance criterion 2 greps this file for exactly
that shape of expression and requires no match.

A SECOND WARNING THE ADAPTERS FORCE ON US: `Balance.locked` is ALWAYS
`0.0` on Kalshi (`app/venues/kalshi/adapter.py` — PLAN.md pins no field
for margin held against resting orders and the adapter refused to invent
one). So a venue-reported `available` must NOT be read as already net of
resting exposure. This ledger's own `locked` accounting is the only
thing in the system tracking capital committed to open orders, which is
why `reserve()` moves money out of `available` at REQUEST time rather
than waiting for a fill.

NO DATABASE, NO SESSION, NO I/O
-------------------------------
`CapitalLedger` is a pure in-memory value object. It takes no
`Session`/`async_sessionmaker`, opens no connection, and awaits nothing —
it is seeded by whoever constructs it (`from_balances()` from a live
`adapter.get_balance()`, or `paper()` from
`Settings.paper_starting_balances`) and mutated only through its own
methods. That is deliberate: the Phase 1 review found the backtester
carries a single pooled `cash` float with no venue dimension, while T18's
cross-venue sizing assumes a per-venue snapshot. The orchestrator's
ruling is that THIS class is the shared abstraction for both the router
and the backtester's `Portfolio`, so it must be usable inside a replay
loop that has no database at all. Every persistence concern (which
`Order`/`Trade`/`Position` rows exist) lives in `app/execution/router.py`.

Units (GUARDRAILS.md §4): every amount in this module is USD.
"""
import logging
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import count
from types import MappingProxyType

from app.config import Settings
from app.config import settings as _default_settings
from app.venues.types import Balance, VenueId

logger = logging.getLogger(__name__)

#: Opaque handle returned by `CapitalLedger.reserve()` and consumed by
#: `release()`/`settle()`. Opaque on purpose: a caller must not be able
#: to construct one, because holding a reservation id is what proves the
#: capital was actually set aside.
ReservationId = str

#: Absolute tolerance (USD) for the "is this amount effectively zero /
#: does this fit" comparisons below. Cash figures here are sums of
#: `size * price` products over book levels, so chained float64 rounding
#: accumulates at roughly 1e-15 USD on realistic notionals; 1e-9 USD is
#: six orders of magnitude above that noise and seven below one cent, so
#: it can never mask a real shortfall a venue would care about.
_CASH_EPSILON = 1e-9


class LedgerError(Exception):
    """Base class for every `CapitalLedger` failure.

    Deliberately NOT a subclass of `app.venues.base.VenueError`: a
    capital failure is a local accounting decision made before any venue
    is contacted, and must never be swallowed by an `except VenueError`
    handler written to retry a flaky venue call.
    """


class InsufficientCapital(LedgerError):
    """A venue does not hold enough free capital for a requested amount.

    Attributes:
        venue: The venue that came up short. Named explicitly because
            the correct response is NEVER "draw the difference from the
            other venue" (GUARDRAILS.md §1.6) — it is to downsize or to
            decline this intent.
        requested: USD requested.
        available: USD actually free on `venue` at the time.
    """

    def __init__(self, venue: VenueId, requested: float, available: float) -> None:
        """Record which venue was short, by how much."""
        super().__init__(
            f"venue {venue!r} has {available!r} USD free but {requested!r} was "
            "requested; capital is per venue and is never drawn from another "
            "venue's balance (GUARDRAILS.md §1.6)"
        )
        self.venue = venue
        self.requested = requested
        self.available = available


class UnknownVenue(LedgerError):
    """A venue was addressed that this ledger was never seeded for.

    Raised rather than defaulting to a zero balance: an unseeded venue
    that silently reads as "no capital" is indistinguishable from a real
    empty account, and would turn a configuration mistake into a run
    where every order on that venue is declined for the wrong reason.
    """


class UnknownReservation(LedgerError):
    """A reservation id was released/settled that this ledger does not hold.

    Also raised for a DOUBLE release/settle: a reservation is removed the
    moment it is resolved, so the second call cannot find it. That is the
    point — releasing the same reservation twice would credit the same
    dollars back twice.
    """


@dataclass(frozen=True)
class VenueCapital:
    """One venue's capital position, as a snapshot value.

    Attributes:
        venue: `"polymarket"` or `"kalshi"`.
        available: USD free to commit to a new order, `>= 0`.
        locked: USD committed to outstanding reservations, `>= 0`.
    """

    venue: VenueId
    available: float
    locked: float

    @property
    def total(self) -> float:
        """Return this ONE venue's `available + locked`, in USD.

        This is a within-venue total and nothing else. There is
        deliberately no ledger-wide equivalent: adding two venues'
        capital together produces a number that cannot be spent
        (GUARDRAILS.md §1.6), and having it available as a property is
        how it ends up sizing an order by accident.
        """
        return self.available + self.locked


@dataclass(frozen=True)
class Reservation:
    """Capital set aside on ONE venue for ONE pending order leg.

    Attributes:
        id: Opaque handle; pass it to `release()` or `settle()`.
        venue: The venue whose `available` was debited.
        amount: USD moved from `available` into `locked`.
    """

    id: ReservationId
    venue: VenueId
    amount: float


class CapitalLedger:
    """Tracks free and committed USD, separately, per venue.

    LIFECYCLE OF A DOLLAR: it starts in `available`; `reserve()` moves it
    to `locked`; then exactly one of `release()` (the order never
    happened — the dollar goes back to `available`) or `settle()` (the
    order filled — the dollars actually spent leave the ledger, the
    remainder returns to `available`) resolves it. `credit()` puts sale
    proceeds back into `available`. There is no path by which a dollar
    changes venue.

    Not thread-safe and not async-safe: it is a plain object with no
    locking, intended to be owned by one router (or one backtest replay
    loop) at a time. Concurrency is the owner's problem, exactly as it is
    for the venue account this mirrors.
    """

    def __init__(self, balances: Mapping[VenueId, float] | None = None) -> None:
        """Seed the ledger with each venue's free USD.

        Args:
            balances: Venue -> free USD. Copied, so a later mutation of
                the caller's mapping cannot change a seeded balance.
                Every value must be finite and `>= 0`. `None` seeds
                nothing; use `seed()` afterwards.

        Raises:
            ValueError: If any balance is not a finite value `>= 0`.
        """
        self._available: dict[VenueId, float] = {}
        self._locked: dict[VenueId, float] = {}
        self._reservations: dict[ReservationId, Reservation] = {}
        self._ids = count(1)
        for venue, amount in (balances or {}).items():
            self.seed(venue, amount)

    # -- Construction ---------------------------------------------------

    @classmethod
    def from_balances(cls, balances: Iterable[Balance]) -> "CapitalLedger":
        """Seed from venue-reported `Balance` objects (the live path).

        The venue's own `locked` is carried across, but see the module
        docstring: Kalshi always reports `locked=0.0`, so on that venue
        this ledger's `locked` will be entirely self-accounted. Do not
        read a zero `locked` as proof of no resting exposure.

        Args:
            balances: One `Balance` per venue, e.g. from
                `await adapter.get_balance()`.

        Returns:
            CapitalLedger: A ledger seeded per venue.
        """
        ledger = cls()
        for balance in balances:
            ledger.seed(balance.venue, balance.available, locked=balance.locked)
        return ledger

    @classmethod
    def paper(cls, settings_obj: Settings | None = None) -> "CapitalLedger":
        """Seed from `Settings.paper_starting_balances` (the paper path).

        Paper mode has no venue account to read, so the starting capital
        is configuration. It is per venue for the same reason everything
        else here is: a paper run that could quietly spend $2,000 of
        "combined" capital on one venue would prove an execution path
        that cannot exist live.

        Args:
            settings_obj: `Settings` to read from; defaults to the
                process-wide singleton. Tests pass an explicit
                `Settings(...)` (GUARDRAILS.md §1.2).

        Returns:
            CapitalLedger: A ledger seeded from
                `paper_starting_balances`.
        """
        cfg = settings_obj if settings_obj is not None else _default_settings
        ledger = cls()
        for venue, amount in cfg.paper_starting_balances.items():
            # `Settings._check_paper_starting_balances` already rejected
            # any key outside `VenueId`, so this cast is checked at load.
            ledger.seed(venue, amount)  # type: ignore[arg-type]
        return ledger

    def seed(self, venue: VenueId, available: float, locked: float = 0.0) -> None:
        """Set (not add to) one venue's balances.

        Args:
            venue: `"polymarket"` or `"kalshi"`.
            available: Free USD, finite and `>= 0`.
            locked: Committed USD, finite and `>= 0`.

        Raises:
            ValueError: If either amount is not finite and `>= 0`.
        """
        _check_amount(available, field="available")
        _check_amount(locked, field="locked")
        self._available[venue] = float(available)
        self._locked[venue] = float(locked)

    # -- Reading --------------------------------------------------------

    @property
    def venues(self) -> tuple[VenueId, ...]:
        """Return the venues this ledger has been seeded for."""
        return tuple(self._available)

    def available(self, venue: VenueId) -> float:
        """Return one venue's free USD.

        Args:
            venue: `"polymarket"` or `"kalshi"`.

        Returns:
            float: Free USD on `venue` only.

        Raises:
            UnknownVenue: If `venue` was never seeded.
        """
        self._require(venue)
        return self._available[venue]

    def locked(self, venue: VenueId) -> float:
        """Return one venue's USD committed to outstanding reservations.

        Args:
            venue: `"polymarket"` or `"kalshi"`.

        Returns:
            float: Committed USD on `venue` only.

        Raises:
            UnknownVenue: If `venue` was never seeded.
        """
        self._require(venue)
        return self._locked[venue]

    def balance(self, venue: VenueId) -> Balance:
        """Return one venue's position as a normalized `Balance`.

        Args:
            venue: `"polymarket"` or `"kalshi"`.

        Returns:
            Balance: `available`/`locked` for `venue`.

        Raises:
            UnknownVenue: If `venue` was never seeded.
        """
        self._require(venue)
        return Balance(
            venue=venue, available=self._available[venue], locked=self._locked[venue]
        )

    def snapshot(self) -> Mapping[VenueId, VenueCapital]:
        """Return an immutable per-venue view of the whole ledger.

        Returns:
            Mapping[VenueId, VenueCapital]: Read-only, one entry per
                seeded venue. Deliberately a MAPPING and not a scalar:
                there is no such thing as "the" balance of this ledger.
        """
        return MappingProxyType(
            {
                venue: VenueCapital(
                    venue=venue,
                    available=self._available[venue],
                    locked=self._locked[venue],
                )
                for venue in self._available
            }
        )

    def available_by_venue(self) -> dict[VenueId, float]:
        """Return `{venue: free USD}` — the snapshot T18's sizing consumes.

        PLAN.md D8 sizes a cross-venue intent as `min(available_A /
        ask_A, available_B / ask_B, max_contracts)`; this is the input to
        that. It is a mapping, never a scalar, so the caller is forced to
        pick a venue before it can spend anything.

        Returns:
            dict[VenueId, float]: A fresh dict, safe for the caller to
                mutate.
        """
        return dict(self._available)

    def min_available(self, venues: Iterable[VenueId]) -> float:
        """Return the SMALLEST free balance across `venues`.

        This is the correct cross-venue bound and the reason no additive
        equivalent exists anywhere in this module: a two-leg intent
        needing capital on both venues can only be as large as the
        poorer venue allows, because the richer venue's surplus cannot
        reach the other leg inside the trade (PLAN.md D8/R6).

        Args:
            venues: Venues the intent needs capital on. Must be non-empty.

        Returns:
            float: `min(available(v) for v in venues)`.

        Raises:
            ValueError: If `venues` is empty.
            UnknownVenue: If any venue was never seeded.
        """
        balances = [self.available(venue) for venue in venues]
        if not balances:
            raise ValueError("min_available requires at least one venue")
        return min(balances)

    def reservation(self, reservation_id: ReservationId) -> Reservation:
        """Return an outstanding reservation by id.

        Args:
            reservation_id: Handle from `reserve()`.

        Returns:
            Reservation: The outstanding reservation.

        Raises:
            UnknownReservation: If it does not exist (or was already
                released/settled).
        """
        try:
            return self._reservations[reservation_id]
        except KeyError:
            raise UnknownReservation(
                f"no outstanding reservation {reservation_id!r}"
            ) from None

    def open_reservations(self) -> tuple[Reservation, ...]:
        """Return every outstanding reservation, in creation order."""
        return tuple(self._reservations.values())

    # -- Mutating -------------------------------------------------------

    def reserve(self, venue: VenueId, amount: float) -> ReservationId:
        """Move `amount` USD from `venue`'s `available` into `locked`.

        Called BEFORE an order is placed, not after it fills: between
        those two moments the capital is genuinely committed, and a
        second intent that spent it would be spending money the venue has
        already earmarked. Kalshi's always-zero venue-reported `locked`
        (module docstring) means this ledger is the only place that
        commitment is visible at all.

        Args:
            venue: The venue whose capital is committed.
            amount: USD to commit; finite and `>= 0`. A zero reservation
                is legal and useful — a SELL leg needs no capital but
                still wants a handle so the caller's release/settle
                bookkeeping stays uniform.

        Returns:
            ReservationId: Handle for `release()`/`settle()`.

        Raises:
            ValueError: If `amount` is not finite and `>= 0`.
            UnknownVenue: If `venue` was never seeded.
            InsufficientCapital: If `venue` does not hold `amount` free.
                NEVER falls back to another venue's balance.
        """
        _check_amount(amount, field="amount")
        self._require(venue)
        free = self._available[venue]
        if amount > free + _CASH_EPSILON:
            raise InsufficientCapital(venue, amount, free)
        take = min(amount, free)
        self._available[venue] = free - take
        self._locked[venue] += take
        reservation = Reservation(
            id=f"res-{next(self._ids)}", venue=venue, amount=take
        )
        self._reservations[reservation.id] = reservation
        logger.info(
            "ledger",
            extra={
                "event": "reserve",
                "venue": venue,
                "reservation_id": reservation.id,
                "amount": take,
                "venue_available": self._available[venue],
                "venue_locked": self._locked[venue],
            },
        )
        return reservation.id

    def release(self, reservation_id: ReservationId) -> float:
        """Return a reservation's capital to `available` unspent.

        Args:
            reservation_id: Handle from `reserve()`.

        Returns:
            float: USD returned to `available`.

        Raises:
            UnknownReservation: If the id is unknown or already resolved.
        """
        reservation = self._pop(reservation_id)
        self._locked[reservation.venue] -= reservation.amount
        self._available[reservation.venue] += reservation.amount
        self._clamp(reservation.venue)
        logger.info(
            "ledger",
            extra={
                "event": "release",
                "venue": reservation.venue,
                "reservation_id": reservation.id,
                "amount": reservation.amount,
                "venue_available": self._available[reservation.venue],
                "venue_locked": self._locked[reservation.venue],
            },
        )
        return reservation.amount

    def settle(self, reservation_id: ReservationId, spent: float) -> float:
        """Resolve a reservation: `spent` USD leaves, the rest comes back.

        `spent` is the FULL cash outlay including fees — for a BUY that
        is `filled_size * avg_price + total_fee`
        (`app.execution.fill_engine.FillResult` documents that the fee is
        a separate positive cost and is NOT netted into `avg_price`).

        An overspend (`spent` greater than the reservation) is tolerated
        rather than rejected, because it is a real outcome: the fee model
        is nonlinear in price (`rate * size * p * (1 - p)`, peaking at
        `p = 0.5`), so a BUY whose limit was 0.90 but which filled at
        0.55 genuinely costs more fee than a reservation sized at the
        limit anticipated. The excess is drawn from the SAME venue's
        `available` and logged at WARNING. It is never drawn from another
        venue (GUARDRAILS.md §1.6), and if that venue cannot cover it the
        call raises rather than letting a balance go negative.

        Args:
            reservation_id: Handle from `reserve()`.
            spent: USD actually spent; finite and `>= 0`.

        Returns:
            float: USD returned to `available` (`0.0` on an overspend).

        Raises:
            ValueError: If `spent` is not finite and `>= 0`.
            UnknownReservation: If the id is unknown or already resolved.
            InsufficientCapital: If `spent` exceeds the reservation AND
                the venue's remaining `available` cannot cover the
                excess.
        """
        _check_amount(spent, field="spent")
        reservation = self._pop(reservation_id)
        venue = reservation.venue
        overspend = spent - reservation.amount
        if overspend > _CASH_EPSILON:
            free = self._available[venue]
            if overspend > free + _CASH_EPSILON:
                # Put the reservation back so the caller's bookkeeping is
                # not left holding a handle this ledger has forgotten.
                self._reservations[reservation.id] = reservation
                raise InsufficientCapital(venue, overspend, free)
            self._available[venue] = free - min(overspend, free)
            self._locked[venue] -= reservation.amount
            returned = 0.0
            logger.warning(
                "ledger",
                extra={
                    "event": "settle_overspend",
                    "venue": venue,
                    "reservation_id": reservation.id,
                    "amount": reservation.amount,
                    "spent": spent,
                    "overspend": overspend,
                },
            )
        else:
            returned = max(0.0, reservation.amount - spent)
            self._locked[venue] -= reservation.amount
            self._available[venue] += returned
        self._clamp(venue)
        logger.info(
            "ledger",
            extra={
                "event": "settle",
                "venue": venue,
                "reservation_id": reservation.id,
                "amount": reservation.amount,
                "spent": spent,
                "returned": returned,
                "venue_available": self._available[venue],
                "venue_locked": self._locked[venue],
            },
        )
        return returned

    def credit(self, venue: VenueId, amount: float) -> None:
        """Add sale proceeds (or a settlement payout) to `venue`'s `available`.

        Args:
            venue: The venue that received the cash. A SELL's proceeds
                land on the venue the contracts were held on — there is
                no other venue they could land on.
            amount: USD received, net of fees; finite and `>= 0`.

        Raises:
            ValueError: If `amount` is not finite and `>= 0`.
            UnknownVenue: If `venue` was never seeded.
        """
        _check_amount(amount, field="amount")
        self._require(venue)
        self._available[venue] += amount
        logger.info(
            "ledger",
            extra={
                "event": "credit",
                "venue": venue,
                "amount": amount,
                "venue_available": self._available[venue],
                "venue_locked": self._locked[venue],
            },
        )

    def debit(self, venue: VenueId, amount: float) -> None:
        """Remove `amount` USD from `venue`'s `available` with no reservation.

        For costs that are not an order's outlay — redemption gas, a
        transfer fee — where there was nothing to reserve against.

        Args:
            venue: The venue charged.
            amount: USD to remove; finite and `>= 0`.

        Raises:
            ValueError: If `amount` is not finite and `>= 0`.
            UnknownVenue: If `venue` was never seeded.
            InsufficientCapital: If `venue` does not hold `amount` free.
        """
        _check_amount(amount, field="amount")
        self._require(venue)
        free = self._available[venue]
        if amount > free + _CASH_EPSILON:
            raise InsufficientCapital(venue, amount, free)
        self._available[venue] = free - min(amount, free)
        logger.info(
            "ledger",
            extra={
                "event": "debit",
                "venue": venue,
                "amount": amount,
                "venue_available": self._available[venue],
                "venue_locked": self._locked[venue],
            },
        )

    # -- Internals ------------------------------------------------------

    def _require(self, venue: VenueId) -> None:
        """Raise `UnknownVenue` unless `venue` has been seeded."""
        if venue not in self._available:
            raise UnknownVenue(
                f"ledger was not seeded for venue {venue!r} (seeded: "
                f"{sorted(self._available)}); refusing to treat it as a zero "
                "balance, which is indistinguishable from a real empty account"
            )

    def _pop(self, reservation_id: ReservationId) -> Reservation:
        """Remove and return a reservation, raising if it is not held."""
        try:
            return self._reservations.pop(reservation_id)
        except KeyError:
            raise UnknownReservation(
                f"no outstanding reservation {reservation_id!r}; it was never "
                "created, or has already been released/settled (resolving one "
                "twice would credit the same dollars back twice)"
            ) from None

    def _clamp(self, venue: VenueId) -> None:
        """Snap sub-epsilon float dust in one venue's balances to zero."""
        if abs(self._locked[venue]) < _CASH_EPSILON:
            self._locked[venue] = 0.0
        if abs(self._available[venue]) < _CASH_EPSILON:
            self._available[venue] = 0.0


def _check_amount(value: float, *, field: str) -> None:
    """Raise `ValueError` unless `value` is a finite USD amount `>= 0`.

    `value < 0` alone is not enough: `float('nan') < 0` and
    `float('inf') < 0` are both `False`, so a naive negativity guard lets
    NaN and Infinity through — and a NaN balance silently poisons every
    later comparison (`nan > x` is always `False`, so every reservation
    would appear to fit). Same rationale as
    `app.venues.types._check_size`.

    Args:
        value: The amount to validate, in USD.
        field: Name of the field, used in the message.

    Raises:
        ValueError: If `value` is not finite or is negative.
    """
    if not (math.isfinite(value) and value >= 0.0):
        raise ValueError(f"{field} must be a finite USD value >= 0, got {value!r}")
