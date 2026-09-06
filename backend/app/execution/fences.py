"""The live-trading fence (PLAN.md D13).

`assert_live_allowed()` is called at the very top of every live venue
adapter's constructor — `app/venues/polymarket/live.py::
PolymarketLiveAdapter.__init__` (T11) and, later, `app/venues/kalshi/
live.py::KalshiLiveAdapter.__init__` (T12). GUARDRAILS.md §1.1/§1.2:
constructing an object that COULD place a real order must itself be
refused unless both of the following are true, together and
deliberately:

  1. `Settings.trading_mode == "live"`
  2. `Settings.live_trading_confirmation == "I_UNDERSTAND_REAL_MONEY"`

Neither condition alone is sufficient — `trading_mode == "live"` with no
confirmation string, or a correct confirmation string while
`trading_mode == "paper"`, both still raise. This is intentional: a
single stray env var (e.g. an operator setting `TRADING_MODE=live` to
test something unrelated) must never be enough, by itself, to arm order
placement.

GUARDRAILS.md §1.2: never set `TRADING_MODE=live` or
`LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_REAL_MONEY` in the shell, `.env`,
a fixture, or a `Settings(...)` instance that reaches the venue registry.
Tests that need to exercise either branch of this fence construct an
explicit `Settings(...)` object and pass it in via `settings_obj`
(the pattern GUARDRAILS.md §1.2 prescribes), rather than mutating
`os.environ` — which the cached, process-wide `app.config.settings`
singleton would not even see, since `app.config.get_settings()` is
`@lru_cache`d and already evaluated by the time any test runs.

T13 (PLAN.md D13) adds two more things, both still evaluated against the
same `Settings` instance the caller already passed in or defaulted to:

  1. A KILL SWITCH: `assert_live_allowed()` also raises
     `KillSwitchEngaged` if `Settings.kill_switch_path` exists as a
     file, checked ONLY once `trading_mode`/`live_trading_confirmation`
     already passed -- so an engaged kill switch is one more thing that
     must be false, not a substitute for the other two. Existence is
     the entire signal: the file's contents are never read, so an
     empty file, a garbage file, or a permissions-denied file all
     engage it identically, and only deleting it disengages it. This
     lives INSIDE `assert_live_allowed()` (not a separate check callers
     might forget) so it guards the same constructor call every live
     adapter already makes -- an engaged kill switch makes a live
     adapter unconstructible, not merely makes its first order fail.
  2. `check_order_limits()`, a SEPARATE function (not folded into
     `assert_live_allowed()`, which only ever guards live-adapter
     CONSTRUCTION): per-order and per-account USD notional/loss limits,
     called from BOTH the paper and live order paths, every time an
     order is about to be placed -- GUARDRAILS.md: these limits are
     exercised in paper mode too, not only once real money is at risk.

THE KILL SWITCH IS CHECKED IN TWO PLACES, FOR TWO DIFFERENT REASONS
--------------------------------------------------------------------
`assert_live_allowed()` (CONSTRUCTION) is not enough on its own, and
relying on it alone was a defect: `app/api/deps.py` caches one
`OrderRouter` and its adapters process-wide, so a construction-time
check runs ONCE per process -- at the first order -- and throwing the
switch afterwards changed nothing at all. Worse, it was inverted: the
only code that re-entered `get_adapter()` on a schedule was the
READ-ONLY reconciliation beat, so an engaged switch stopped the pass an
operator most wants during a halt while cached routers kept placing.

`assert_placement_allowed()` (PLACEMENT) is the fix and is the
authoritative halt. `OrderRouter.submit()` calls it before any leg is
planned, reserved or placed, in BOTH paper and live -- a halt that only
applied to real money could never be rehearsed. It deliberately does NOT
consider `trading_mode`/`live_trading_confirmation`: "is live armed" and
"is trading halted" are different questions, and conflating them is what
made a placement fence answer a reconciliation question.

Reconciliation is separated from BOTH: `app.tasks.execution` asks
`app.venues.registry.get_read_adapter()` for an adapter that has no
`place_order` at all, so it never evaluates a placement fence and keeps
running while the switch is engaged. Nothing here halts a CANCEL, either
-- cancelling reduces exposure, which is the direction a halt wants.
"""
import logging
from pathlib import Path

from app.config import Settings
from app.config import settings as _default_settings

logger = logging.getLogger(__name__)

#: The exact string an operator must set `Settings.live_trading_confirmation`
#: to, IN ADDITION TO `Settings.trading_mode == "live"`, before a live venue
#: adapter can be constructed. Deliberately long, specific, and shouty so it
#: can never be set by accident — a stray `LIVE_TRADING_CONFIRMATION=true` or
#: `=yes` does not match this.
LIVE_TRADING_CONFIRMATION_PHRASE = "I_UNDERSTAND_REAL_MONEY"


class LiveTradingDisabled(Exception):
    """Raised by `assert_live_allowed()` when live trading is not enabled.

    Deliberately NOT a subclass of `app.venues.base.VenueError`: this is a
    configuration fence raised before any venue is ever contacted, not a
    venue response, and it must never be silently swallowed by an
    `except VenueError` handler written to retry a flaky venue call.
    """


class KillSwitchEngaged(Exception):
    """Raised by `assert_live_allowed()` when the kill-switch file exists.

    Deliberately a DIFFERENT exception from `LiveTradingDisabled`: the
    latter means "live trading was never armed"; this one means "live
    trading WAS armed, deliberately, and an operator (or an automated
    guard) has since thrown the kill switch" -- callers that want to
    distinguish "not configured for live" from "configured for live, but
    halted" can catch these separately. Also not a subclass of
    `app.venues.base.VenueError`, for the same reason as
    `LiveTradingDisabled`.
    """


def assert_live_allowed(settings_obj: Settings | None = None) -> None:
    """Raise unless live trading is deliberately enabled and not halted.

    Args:
        settings_obj: The `Settings` instance to check against. Defaults
            to the process-wide `app.config.settings` singleton. Tests
            pass an explicit `Settings(...)` here (GUARDRAILS.md §1.2)
            instead of mutating `os.environ` or the cached singleton, so
            a test proving the "allowed" branch never has to set
            `TRADING_MODE=live` in the actual test process.

    Raises:
        LiveTradingDisabled: If `settings_obj.trading_mode != "live"` or
            `settings_obj.live_trading_confirmation !=
            LIVE_TRADING_CONFIRMATION_PHRASE`. Both conditions are
            evaluated (not short-circuited) so the raised message always
            states every reason the fence tripped, not just the first one
            checked.
        KillSwitchEngaged: If both of the above hold (trading_mode="live"
            AND the confirmation phrase matches) but
            `settings_obj.kill_switch_path` exists as a file. Checked
            ONLY once the fence would otherwise open -- a kill switch
            with live trading never armed is a no-op, not a distinct
            error -- and checked by EXISTENCE alone: the file's contents
            are never read or parsed, so there is no "right" text to put
            in it and no way for an unreadable-but-present file to be
            mistaken for an absent one. Relative paths resolve against
            the process's current working directory (Python's ordinary
            filesystem-path resolution, applied nowhere else in this
            function); pass an absolute path (e.g. under `tmp_path`) to
            keep a check deterministic and independent of CWD.
    """
    cfg = settings_obj if settings_obj is not None else _default_settings
    mode_ok = cfg.trading_mode == "live"
    confirmed = cfg.live_trading_confirmation == LIVE_TRADING_CONFIRMATION_PHRASE
    if mode_ok and confirmed:
        if Path(cfg.kill_switch_path).exists():
            raise KillSwitchEngaged(
                f"Kill switch engaged: {cfg.kill_switch_path!r} exists. "
                "Live trading is halted until that file is removed."
            )
        return
    reasons: list[str] = []
    if not mode_ok:
        reasons.append(f"trading_mode={cfg.trading_mode!r} (must be 'live')")
    if not confirmed:
        reasons.append(
            "live_trading_confirmation does not match the required phrase"
        )
    raise LiveTradingDisabled("Live trading is disabled: " + "; ".join(reasons))


def kill_switch_engaged(settings_obj: Settings | None = None) -> bool:
    """Return `True` if the kill-switch file exists.

    Existence is the entire signal (same rule as `assert_live_allowed`):
    the file's contents are never read, so an empty file, a garbage
    file, or a permissions-denied file all engage it identically.

    Args:
        settings_obj: The `Settings` whose `kill_switch_path` is checked.
            Defaults to the process-wide `app.config.settings` singleton;
            tests pass an explicit `Settings(...)` with an absolute
            `tmp_path` (GUARDRAILS.md §1.2).

    Returns:
        bool: `True` if the switch is engaged.
    """
    cfg = settings_obj if settings_obj is not None else _default_settings
    return Path(cfg.kill_switch_path).exists()


def assert_placement_allowed(settings_obj: Settings | None = None) -> None:
    """Raise `KillSwitchEngaged` if order placement is currently halted.

    Called by `app.execution.router.OrderRouter.submit()` BEFORE any leg
    is planned, reserved or placed, in BOTH paper and live mode. See this
    module's docstring for why the construction-time check in
    `assert_live_allowed()` cannot carry this on its own (the router and
    its adapters are process-wide singletons, so that check runs once,
    at the first order, and never again).

    Mode is deliberately NOT considered here. "Is live trading armed?"
    is `assert_live_allowed()`'s question and is answered once, when an
    adapter is constructed; "is trading halted right now?" is this
    function's question and must be answered on every order. A halt that
    could not be exercised in paper would be a halt nobody had ever
    tested.

    Args:
        settings_obj: The `Settings` whose `kill_switch_path` is checked.
            Defaults to the process-wide singleton.

    Raises:
        KillSwitchEngaged: If `Settings.kill_switch_path` exists.
    """
    cfg = settings_obj if settings_obj is not None else _default_settings
    if kill_switch_engaged(cfg):
        raise KillSwitchEngaged(
            f"Kill switch engaged: {cfg.kill_switch_path!r} exists. Order "
            "placement is halted until that file is removed. Reads, "
            "reconciliation and cancellations are unaffected."
        )


class RiskLimitExceeded(Exception):
    """Raised by `check_order_limits()` when a configured risk cap would be
    breached.

    Deliberately a separate exception from `LiveTradingDisabled`/
    `KillSwitchEngaged`: those two guard whether a live adapter can be
    CONSTRUCTED at all; this one guards whether one SPECIFIC order should
    be placed, and it is raised from BOTH the paper and live order paths
    (GUARDRAILS.md: these limits are exercised in paper too), so it must
    not be confused with -- or accidentally caught by -- a handler
    written for the construction-time fences.
    """


#: The only bucket `check_order_limits` currently gives special
#: treatment to (PLAN.md D10(d), T20). A `bucket` argument outside this
#: set is accepted but has NO additional effect — see the function
#: docstring for why this is deliberately not a closed/validated enum.
_NEAR_RESOLUTION_BUCKET = "near_resolution"

#: Every bucket tag anything in this system knows how to act on. It is a
#: KNOWN set, not a VALIDATED one: an unrecognized tag still passes
#: (`check_order_limits` never raises over a bucket name — see its
#: docstring), it is merely made LOUD by `warn_unknown_bucket`.
#:
#: WHY THIS EXISTS AT ALL. The bucket cap is matched with `==`, so every
#: spelling but the exact one — `"Near_Resolution"`, `"NEAR_RESOLUTION"`,
#: `" near_resolution"`, `"near-resolution"`, `""` — silently skips the
#: check: a strategy that typoes its own tag removes its own cap and
#: nothing anywhere says so. `app.execution.router.OrderRouter
#: ._bucket_open_notional` would even go on aggregating that bucket's
#: exposure faithfully, for a fence that will never read it. Refusing
#: the order was rejected as the fix (a typo must not crash the
#: order-placement path); a WARNING naming the tag is the fix, so the
#: mistake is visible in operation instead of costing money silently.
#:
#: Tags are compared EXACTLY, with no normalization: casefolding or
#: stripping here would have to be mirrored in `_bucket_open_notional`'s
#: aggregation and in `IntentRecord.extra_data["bucket"]` or the fence
#: and the aggregate would disagree about which rows are in the bucket —
#: a worse failure than the one being fixed.
KNOWN_BUCKETS: frozenset[str] = frozenset({_NEAR_RESOLUTION_BUCKET})


def warn_unknown_bucket(bucket: str | None, *, source: str) -> bool:
    """Log a WARNING for a bucket tag no fence will ever act on.

    Never raises and never changes what is placed — the caller's order
    proceeds exactly as it would have. The point is only that an
    uncapped bucket stops being silent.

    Args:
        bucket: The tag as it was supplied. `None` means "no bucket",
            which is a legitimate, deliberate state (most intents carry
            no tag at all) and is NOT warned about.
        source: Where the tag entered — e.g. `"check_order_limits"` or
            `"OrderRouter._check_limits"` — so an operator reading the
            log knows which path saw it.

    Returns:
        bool: `True` if `bucket` is `None` or a member of
            `KNOWN_BUCKETS`; `False` if it was unrecognized (and
            therefore warned about). Callers use the return value to
            skip work that only a recognized bucket could ever use.
    """
    if bucket is None or bucket in KNOWN_BUCKETS:
        return True
    logger.warning(
        "unrecognized bucket tag %r: NO bucket cap will be applied to it. "
        "Known buckets: %s. A near-certain cause is a typo in a strategy's "
        "metadata['bucket'] — the tag is matched exactly.",
        bucket,
        sorted(KNOWN_BUCKETS),
        extra={
            "event": "unknown_bucket_tag",
            "bucket": bucket,
            "source": source,
            "known_buckets": sorted(KNOWN_BUCKETS),
        },
    )
    return False


def check_order_limits(
    order_notional: float,
    open_notional: float,
    daily_pnl: float,
    settings_obj: Settings | None = None,
    *,
    bucket: str | None = None,
    bucket_notional: float = 0.0,
) -> None:
    """Raise `RiskLimitExceeded` if this order would breach a risk limit.

    Called immediately before an order is placed -- by BOTH the paper and
    live order paths (GUARDRAILS.md: limits are exercised in paper too,
    not only once real money is at risk). Every limit is a strict
    "exceeds", not "reaches": a value sitting exactly ON the configured
    cap passes, so an operator who sets a limit to a round number they
    actually intend to allow (e.g. `MAX_ORDER_NOTIONAL_USD=250` allowing
    an order of exactly $250.00) is not tripped by their own number.

    THE BUCKET CHECK (`bucket`/`bucket_notional`, T20, PLAN.md D10(d)) is
    a FOURTH, independent limit, additive to the three below: it exists
    because a near-resolution capital-lockup trade
    (`app.strategies.settlement_edge`) ties up capital for the DURATION
    of a settlement delay, not for as long as an ordinary position sits
    open, so PLAN.md gives it its own, tighter cap
    (`max_near_resolution_notional_usd`) on top of the general
    `max_open_notional_usd`. Unlike `open_notional` (a single account-wide
    total this function has no opinion about how to compute), `bucket`
    names WHICH aggregate `bucket_notional` represents — today the only
    bucket with an effect is `"near_resolution"`
    (`app.execution.router.OrderRouter._check_limits` is the one caller,
    and it is the one place that already has both the intent's own
    `metadata["bucket"]` tag and the capital already committed to that
    bucket). `bucket=None` (the default) skips this check entirely, and
    an unrecognized bucket name is accepted as a no-op rather than
    raising — a typo here should never be able to turn an intended
    bucket cap into a crash inside the order-placement path.

    IT IS NO LONGER SILENT, THOUGH. An unrecognized tag used to pass
    with nothing said, so `"Near_Resolution"` (or `" near_resolution"`,
    or `"near-resolution"`, or `""`) let an order of ANY size through a
    bucket cap and the only evidence was the money. Every non-`None` tag
    now goes through `warn_unknown_bucket`, which logs a WARNING naming
    it when it is outside `KNOWN_BUCKETS`. That is the entire change:
    the order still proceeds, this function still never raises over a
    bucket NAME (only over a breached LIMIT), and `KNOWN_BUCKETS` stays
    a known set rather than a validated enum.

    Args:
        order_notional: USD notional of the order about to be placed
            (`size_contracts * limit_price`, summed across an intent's
            legs), NOT counting anything already open. Must be `>= 0`.
        open_notional: USD notional already open (resting orders plus
            open positions) across the account, NOT including
            `order_notional`. Must be `>= 0`.
        daily_pnl: Realized-plus-unrealized USD profit/loss for the
            current trading day. Negative is a loss; this function does
            not itself define "day" or reset this value -- the caller
            supplies whatever it is currently tracking.
        settings_obj: The `Settings` instance whose `max_order_notional_usd`
            / `max_open_notional_usd` / `max_daily_loss_usd` /
            `max_near_resolution_notional_usd` are checked against.
            Defaults to the process-wide `app.config.settings`
            singleton; tests pass an explicit `Settings(...)` (same
            GUARDRAILS.md §1.2 pattern as `assert_live_allowed`).
        bucket: `"near_resolution"` to apply the bucket check against
            `settings_obj.max_near_resolution_notional_usd`; `None`
            (default) skips it silently, and any other value skips it
            with a WARNING (`warn_unknown_bucket`).
        bucket_notional: USD notional already committed to `bucket`
            (across every OTHER order/position tagged with it), NOT
            including `order_notional`. Must be `>= 0`. Ignored unless
            `bucket` is recognized.

    Raises:
        RiskLimitExceeded: If `order_notional > max_order_notional_usd`,
            or `open_notional + order_notional > max_open_notional_usd`,
            or `daily_pnl < -max_daily_loss_usd` (a realized/unrealized
            loss strictly beyond the configured cap), or (when
            `bucket == "near_resolution"`) `bucket_notional +
            order_notional > max_near_resolution_notional_usd`. All
            applicable checks are evaluated (not short-circuited) so the
            message states every limit breached, not just the first one
            checked.
    """
    cfg = settings_obj if settings_obj is not None else _default_settings
    # Warn FIRST, before any limit can raise: an unrecognized tag on an
    # order that trips some OTHER limit is the same typo, and the
    # operator needs to see it either way.
    warn_unknown_bucket(bucket, source="check_order_limits")
    reasons: list[str] = []
    if order_notional > cfg.max_order_notional_usd:
        reasons.append(
            f"order_notional={order_notional!r} exceeds "
            f"max_order_notional_usd={cfg.max_order_notional_usd!r}"
        )
    total_open = open_notional + order_notional
    if total_open > cfg.max_open_notional_usd:
        reasons.append(
            f"open_notional+order_notional={total_open!r} exceeds "
            f"max_open_notional_usd={cfg.max_open_notional_usd!r}"
        )
    loss_cap = -cfg.max_daily_loss_usd
    if daily_pnl < loss_cap:
        reasons.append(
            f"daily_pnl={daily_pnl!r} is below the allowed floor of "
            f"{loss_cap!r} (max_daily_loss_usd={cfg.max_daily_loss_usd!r})"
        )
    if bucket == _NEAR_RESOLUTION_BUCKET:
        total_bucket = bucket_notional + order_notional
        if total_bucket > cfg.max_near_resolution_notional_usd:
            reasons.append(
                f"bucket={bucket!r} notional "
                f"bucket_notional+order_notional={total_bucket!r} exceeds "
                f"max_near_resolution_notional_usd="
                f"{cfg.max_near_resolution_notional_usd!r}"
            )
    if reasons:
        raise RiskLimitExceeded("Risk limit exceeded: " + "; ".join(reasons))
