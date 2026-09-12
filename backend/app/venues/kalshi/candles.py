"""Kalshi candlestick history: a typed client and a no-look-ahead selector.

T1 (mm-proveout PLAN.md D2/D8). The preceding session fetched candles ad
hoc, inline, everywhere it needed history (`app/scripts/calibration.py`'s
`_price`): `adapter._get(f"/series/{series}/markets/{ticker}/"
"candlesticks", params={"start_ts", "end_ts", "period_interval"})`, then
hand-parsed `end_period_ts`/`yes_bid`/`yes_ask`/`price` inline at every
call site. This module is that funnel made typed and single: every
consumer in this kit (T2's replay, T4's calibration sweep, T5's
minute-study) imports `fetch_candles`/`Candle` from here instead of
repeating the parse.

ENDPOINT: `GET /series/{series}/markets/{ticker}/candlesticks
?start_ts=&end_ts=&period_interval=` — `period_interval` in
`{1, 60, 1440}` minutes (PLAN.md §"Verified repo facts"). The response
envelope carries the list under `"candlesticks"`.

FIELD MAPPING, per candlestick object (measured live, `probe_kalshi_
candles.py`):
  * `end_period_ts` -> `Candle.end_ts`. THIS IS THE PERIOD END, not the
    start — see `candle_at_or_before` below for why that is the fact the
    whole no-look-ahead property rests on.
  * `yes_bid.close_dollars` -> `bid_close`; `yes_ask.close_dollars` ->
    `ask_close`. Both are Kalshi's `*_dollars` fixed-point STRING
    encoding, already a probability in `[0,1]` — no `/100` conversion
    (that funnel, `KalshiAdapter._to_dollars`, exists for the bare
    integer-cents encoding, which the candlestick payload does not use
    for these two fields).
  * `price.low_dollars` / `price.high_dollars` / `price.close_dollars`
    -> `px_low` / `px_high` / `px_close`. **The whole `price` object is
    ABSENT when `volume_fp` is `"0.00"`** — no trades printed in the
    period, so there is no trade OHLC to report. That is read here as
    `px_* = None`, deliberately never as a parse error and never as a
    fabricated `0.0` price (GUARDRAILS.md §3.3: a parse that cannot
    honour the payload refuses or reports the gap, it never substitutes
    a plausible-looking default).
  * `volume_fp` -> `volume` (contracts traded in the period; `0.0` when
    the field is absent or unparseable — a candle with no field says the
    same thing as one that spells out `"0.00"`).
  * `open_interest_fp` -> `open_interest`.

Units (GUARDRAILS.md §4): every price here is a probability in `[0,1]`;
`volume`/`open_interest` are in contracts. `end_ts` is a Unix timestamp
in seconds, matching the `start_ts`/`end_ts` query parameters this
module sends.

VALIDATION IS NOT OPTIONAL (T1 retry, GUARDRAILS.md §3.3: "a parse that
cannot honour the payload refuses and falls back with provenance — it
never silently substitutes a default"). Two prior gaps made a corrupt
payload indistinguishable from an innocent one, and both are closed by
reusing the SAME primitives the rest of this repo already trusts for
exactly this job, rather than inventing a second version of either:
  * Every parsed price (`bid_close`/`ask_close`/`px_low`/`px_high`/
    `px_close`) and every size (`volume`/`open_interest`) is range-
    checked in `Candle.__post_init__` via `app.venues.types._check_price`
    / `_check_size` — the identical validators `BookLevel`, `Fill`,
    `Position`, and every other venue-parsed price/size in this repo
    already go through. A `bid_close` of `5.0` or a `volume` of `-5.0`
    is not a plausible Kalshi value silently carried into a `Candle`; it
    is a `VenuePayloadError` (`_parse_candle` re-raises the bare
    `ValueError` these validators raise, the same rewrap
    `KalshiAdapter._build_market` does for `VenueMarket`).
  * The `"candlesticks"` envelope is validated by
    `KalshiAdapter._require_envelope_list` (same module, same helper
    every other Kalshi list-shaped endpoint already uses) rather than
    `body.get("candlesticks") or []`, which could not tell a corrupt
    payload (a string, a dict, a bare `True`) from a market with no
    candles in the window. A non-dict entry inside the list is refused
    exactly as loudly as a dict-shaped entry missing `end_period_ts` —
    one bad candle among good ones aborts the whole fetch rather than
    vanishing silently, because a silently shortened time series is a
    worse failure for a backtest than a raise (`_parse_candle`'s
    docstring, and `test_one_malformed_candle_among_good_ones_does_not_
    silently_vanish`).
  * `yes_bid`/`yes_ask`/`price` being present but a truthy non-dict
    (a wrong-but-truthy type, e.g. a string) raises `VenuePayloadError`
    too, not an `AttributeError` from calling `.get()` on it.

Read-only: this module's only HTTP verb is the `GET` `KalshiAdapter._get`
already restricts itself to (GUARDRAILS.md §1.1) — there is no write
path here to place, modify, or cancel anything.

CHUNKING (defect fix, measured live 2026-09-07 during T5, run
2026-09-07-7465; NOTES.md "T5 — 1-MINUTE SUB-STUDY"). Kalshi's
candlestick endpoint caps a single request at
`MEASURED_MAX_PERIODS_PER_REQUEST` periods: a 5,000-period window
succeeded, a 5,040-period window returned HTTP 400. T5 needed 1-minute
candles over a 10-day (14,400-period) lookback, hit this on its first
attempt, failed 3/3, and worked around it with a 4,800-period chunking
loop in its own scratch driver rather than editing this module — so the
workaround never reached the repo and the next minute-scale task would
have hit the same wall. `fetch_candles` now chunks internally at
`CHUNK_SIZE_PERIODS` (4,800, a margin below the measured 5,000 ceiling)
so every caller keeps the "pass a whole window, get one sorted
`list[Candle]`" contract regardless of window length.

The chunk boundaries touch (`windows[i][1] == windows[i+1][0]`) rather
than overlapping or leaving a gap. This is deliberately NOT a defensive
"overlap and dedupe" scheme: `end_period_ts` is the period END (module
docstring above), and the measured fact that a window of EXACTLY 5,000
periods returns 5,000 candles (not 4,999 or 5,001) is only possible if
the venue treats `[start_ts, end_ts]` as bounding `end_period_ts` on a
half-open interval (`start_ts < end_period_ts <= end_ts`, or the
symmetric exclusive-end form) — under EITHER of those, and only those,
a window split at any point `start_ts <= b <= end_ts` partitions the
candles cleanly: nothing in `(start_ts, b]`/`[start_ts, b)` overlaps
anything in `(b, end_ts]`/`[b, end_ts)`. A fully-inclusive-both-ends or
fully-exclusive-both-ends convention would make the 5,000-exactly
measurement come out at 5,001 or 4,999 instead, which was not observed.
`tests/venues/test_kalshi_candles.py`'s boundary tests pin the resulting
arithmetic with adjacent mock candles at the touch point.

PARTIAL FAILURE (design decision this fix had to make, since chunking
turns one request per market into several): if any chunk's request
raises — `VenuePayloadError` (a chunk parsed but was corrupt) or
`httpx.HTTPError` (a chunk's transport/status failed) — `fetch_candles`
does NOT catch it and return the candles collected from earlier chunks.
It propagates immediately, so the caller sees the SAME exception shape
a single unchunked request already raised, and gets nothing for that
market rather than a truncated series that looks like a genuinely
short-lived market (GUARDRAILS.md §3.3: "a parse that cannot honour the
payload refuses ... it never silently substitutes a default" — a
partial candle history for a market that was actually fully quoted is
exactly that kind of substitution). `app/scripts/mm_backtest.py`'s
`_collect_one` already catches both exception types per-market and
records a `CollectFailure` with the right `kind`; that catch site needed
no change for this to work, since a chunk's exception is the same type
a single-request fetch could already raise.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.utils.time import ensure_aware
from app.venues.base import VenuePayloadError
from app.venues.kalshi.adapter import _require_envelope_list
from app.venues.types import VenueMarket, _check_price, _check_size

if TYPE_CHECKING:
    from app.venues.kalshi.adapter import KalshiAdapter

#: `period_interval` values Kalshi's candlestick endpoint accepts, in
#: minutes (PLAN.md §"Verified repo facts": "`period_interval` in
#: `{1, 60, 1440}` minutes").
VALID_INTERVAL_MINUTES: tuple[int, ...] = (1, 60, 1440)

#: Seconds between consecutive hourly candles' `end_period_ts` when the
#: venue has no gap in its history — `probe_kalshi_candles.py` checks
#: this directly against a live sample.
HOURLY_CANDLE_SPACING_S = 3600

#: Kalshi's candlestick endpoint's measured hard cap on periods per
#: request — measured live 2026-09-07 (T5, run 2026-09-07-7465,
#: NOTES.md "T5 — 1-MINUTE SUB-STUDY"): a window of exactly 5,000
#: periods succeeded; a 5,040-period window returned HTTP 400.
#: `fetch_candles` never requests this many periods in one call — see
#: `CHUNK_SIZE_PERIODS` below — this constant exists only to record WHY
#: that one is set where it is.
MEASURED_MAX_PERIODS_PER_REQUEST = 5000

#: Periods requested per chunk in `fetch_candles`'s window-splitting.
#: Kept a 200-period margin below `MEASURED_MAX_PERIODS_PER_REQUEST`
#: rather than pinned to the ceiling itself, since that ceiling was
#: measured once, on 2026-09-07, against one interval and one market,
#: and is not re-verified on every call this module makes. At
#: `interval_minutes=1` a chunk spans 80 hours; at `60`, 200 days; at
#: `1440` (daily), ~13.2 years.
CHUNK_SIZE_PERIODS = 4800


@dataclass(frozen=True)
class Candle:
    """One Kalshi candlestick, parsed onto this repo's units.

    Attributes:
        end_ts: Unix timestamp (seconds) of the END of this candle's
            period — NOT the start. See `candle_at_or_before`.
        bid_close: Best YES bid at period close, a probability in
            `[0,1]`, or `None` if the venue omitted it.
        ask_close: Best YES ask at period close, a probability in
            `[0,1]`, or `None` if the venue omitted it.
        px_low: Lowest traded price in the period, or `None` when the
            period had no trades (`volume == 0.0`) — see the module
            docstring.
        px_high: Highest traded price in the period, or `None` under the
            same zero-volume condition.
        px_close: Last traded price in the period, or `None` under the
            same zero-volume condition.
        volume: Contracts traded in the period, `>= 0`. `0.0` when the
            venue's `volume_fp` is absent, unparseable, or genuinely
            zero — all three read the same way, because none of them is
            evidence of MORE than zero volume.
        open_interest: Open interest at period close, in contracts, or
            `None` if the venue omitted it.
    """

    end_ts: int
    bid_close: float | None
    ask_close: float | None
    px_low: float | None
    px_high: float | None
    px_close: float | None
    volume: float
    open_interest: float | None

    def __post_init__(self) -> None:
        """Enforce the invariants this class's own docstring states.

        Reuses `app.venues.types._check_price` (probability in `[0,1]`)
        and `_check_size` (finite, `>= 0`) — the same validators every
        other venue-parsed price/size in this repo already goes through
        (`BookLevel`, `Fill`, `Position`, ...) — rather than a second,
        bespoke range check. Raises a bare `ValueError`, exactly as
        those dataclasses' own `__post_init__`s do; `_parse_candle`
        catches it and re-raises `VenuePayloadError`, the same rewrap
        `KalshiAdapter._build_market` performs for `VenueMarket`.

        A `None` field (the venue omitted it, or it is the zero-volume
        candle's absent `price` object — module docstring) is never
        checked: `None` already means "not reported", which is a valid,
        honoured state, not a value to range-check.
        """
        for name, price in (
            ("bid_close", self.bid_close),
            ("ask_close", self.ask_close),
            ("px_low", self.px_low),
            ("px_high", self.px_high),
            ("px_close", self.px_close),
        ):
            if price is not None:
                _check_price(price, field=name)
        _check_size(self.volume, field="volume")
        if self.open_interest is not None:
            _check_size(self.open_interest, field="open_interest")


def _float(value: object) -> float | None:
    """Best-effort finite `float(value)`; `None` on failure/`None`/non-finite.

    Mirrors `KalshiAdapter._try_float` (kept as a local copy rather than
    an import: it is a two-line, single-expression helper, unlike
    `_require_envelope_list` below, which this module DOES import — the
    difference is that `_require_envelope_list` carries real behaviour
    worth keeping in exactly one place, while re-deriving this one is
    cheaper than a cross-module dependency for it).
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _dict_field(raw: dict[str, Any], key: str) -> dict[str, Any]:
    """Return `raw[key]` as a `dict`, `{}` if absent/falsy, else raise.

    `raw.get(key) or {}` (the prior behaviour) guarded `None` and other
    falsy values but not a wrong-but-TRUTHY type — a `yes_bid: "bad"`
    payload made `.get("close_dollars")` raise a bare `AttributeError`
    (`'str' object has no attribute 'get'`), not the `VenuePayloadError`
    every other refusal in this module raises. A falsy-but-present value
    (`None`, `""`, `0`, `False`) is still read as "not reported" — that
    part of the old behaviour was never the bug.

    Args:
        raw: The raw candlestick object.
        key: The field to read (`"yes_bid"`, `"yes_ask"`, or `"price"`).

    Returns:
        dict[str, Any]: `raw[key]` if it is a dict, `{}` if falsy.

    Raises:
        VenuePayloadError: If `raw[key]` is present, truthy, and not a
            `dict`.
    """
    value = raw.get(key)
    if not value:
        return {}
    if not isinstance(value, dict):
        raise VenuePayloadError(
            f"kalshi candlestick {key!r} must be an object, got {value!r}", raw=raw
        )
    return value


def _parse_candle(raw: dict[str, Any]) -> Candle:
    """Parse one raw candlestick object into a `Candle` (module docstring).

    Raises:
        VenuePayloadError: If `end_period_ts` is missing or unparseable
            (a candle with no place in time cannot be sorted or compared
            against a cutoff — `candle_at_or_before`'s whole job — so
            there is no safe default to fall back to); if `yes_bid`/
            `yes_ask`/`price` is present but not an object
            (`_dict_field`); or if a parsed price is outside `[0,1]` or
            a parsed size is negative (`Candle.__post_init__`, rewrapped
            here exactly as `KalshiAdapter._build_market` rewraps
            `VenueMarket`'s own `ValueError` into this type).
    """
    end_ts = _float(raw.get("end_period_ts"))
    if end_ts is None:
        raise VenuePayloadError(
            "kalshi candlestick missing/unparseable end_period_ts", raw=raw
        )
    yes_bid = _dict_field(raw, "yes_bid")
    yes_ask = _dict_field(raw, "yes_ask")
    price = _dict_field(raw, "price")
    try:
        return Candle(
            end_ts=int(end_ts),
            bid_close=_float(yes_bid.get("close_dollars")),
            ask_close=_float(yes_ask.get("close_dollars")),
            px_low=_float(price.get("low_dollars")),
            px_high=_float(price.get("high_dollars")),
            px_close=_float(price.get("close_dollars")),
            volume=_float(raw.get("volume_fp")) or 0.0,
            open_interest=_float(raw.get("open_interest_fp")),
        )
    except ValueError as exc:
        raise VenuePayloadError(
            f"kalshi candlestick did not validate: {exc}", raw=raw
        ) from exc


def _chunk_windows(
    start_ts: int, end_ts: int, *, interval_minutes: int
) -> list[tuple[int, int]]:
    """Split `[start_ts, end_ts]` into consecutive, TOUCHING sub-windows.

    Each sub-window spans at most `CHUNK_SIZE_PERIODS` periods
    (`CHUNK_SIZE_PERIODS * interval_minutes * 60` seconds). Consecutive
    windows touch — `windows[i][1] == windows[i + 1][0]` — rather than
    overlapping or leaving a gap; the module docstring's CHUNKING section
    explains why touching is the correct choice given the venue's
    measured behaviour, not merely the simplest one.

    Args:
        start_ts: Window start, Unix seconds.
        end_ts: Window end, Unix seconds. May be `<= start_ts` (a
            degenerate/empty window) — see the fallback below.
        interval_minutes: Candle granularity in minutes, already
            validated by the caller against `VALID_INTERVAL_MINUTES`.

    Returns:
        list[tuple[int, int]]: One or more `(chunk_start, chunk_end)`
            pairs, in ascending order, covering `[start_ts, end_ts]`
            exactly once each. Always at least one pair — including for
            `end_ts <= start_ts` — so `fetch_candles` keeps issuing
            exactly one request for a degenerate window, matching its
            pre-chunking behaviour (whatever the venue does with a
            zero/negative-length window is unchanged by this fix).
    """
    if end_ts <= start_ts:
        return [(start_ts, end_ts)]
    chunk_span_s = CHUNK_SIZE_PERIODS * interval_minutes * 60
    windows: list[tuple[int, int]] = []
    cursor = start_ts
    while cursor < end_ts:
        chunk_end = min(cursor + chunk_span_s, end_ts)
        windows.append((cursor, chunk_end))
        cursor = chunk_end
    return windows


async def fetch_candles(
    adapter: KalshiAdapter,
    *,
    series: str,
    ticker: str,
    start: datetime,
    end: datetime,
    interval_minutes: int,
) -> list[Candle]:
    """Fetch and parse one market's candlestick history, sorted by `end_ts`.

    Issues one `GET /series/{series}/markets/{ticker}/candlesticks` per
    chunk of at most `CHUNK_SIZE_PERIODS` periods (`_chunk_windows` —
    the module docstring's CHUNKING section explains why one request
    cannot cover an arbitrary window), validates each chunk's
    `"candlesticks"` envelope via `KalshiAdapter._require_envelope_list`
    (present, a list, every element an object — GUARDRAILS.md §3.3: a
    corrupt payload must not be indistinguishable from an empty window),
    parses every element via `_parse_candle`, and concatenates every
    chunk's candles before sorting once at the end. The signature and
    contract are unchanged from before chunking existed: callers still
    pass one whole window and get back one sorted `list[Candle]`.

    Args:
        adapter: A `KalshiAdapter` (live, or test-injected with an
            `httpx.MockTransport` — GUARDRAILS.md §1.4).
        series: Kalshi series ticker, e.g. `"KXFED"` — see `series_for`.
        ticker: Kalshi market ticker (the specific contract).
        start: Aware UTC start of the requested window (`start_ts`).
        end: Aware UTC end of the requested window (`end_ts`).
        interval_minutes: Candle granularity in minutes. Must be one of
            `VALID_INTERVAL_MINUTES` (`{1, 60, 1440}`) — that is the only
            set the venue's `period_interval` parameter accepts.

    Returns:
        list[Candle]: Parsed candles from every chunk, ascending by
            `end_ts`. Empty if the venue returned none for the window.

    Raises:
        ValueError: If `interval_minutes` is not one of
            `VALID_INTERVAL_MINUTES`. Raised before any chunk is
            computed or any request is issued.
        VenuePayloadError: If any chunk's response carries no
            `"candlesticks"` key, or the value under it is not a list of
            objects (`_require_envelope_list`) — including a bare
            `True`, a string, or a dict, none of which is an empty
            window; or if any one candle in any chunk fails to parse
            (`_parse_candle`). Raised immediately on the first chunk
            that fails — candles already collected from EARLIER chunks
            are discarded, not returned, so a mid-fetch failure can
            never come back as a truncated-but-successful history (the
            module docstring's PARTIAL FAILURE section).
        httpx.HTTPError: If any chunk's request fails at the transport or
            HTTP-status level (propagated from `adapter._get`), with the
            same discard-on-failure behaviour as `VenuePayloadError`
            above.
    """
    if interval_minutes not in VALID_INTERVAL_MINUTES:
        raise ValueError(
            f"interval_minutes must be one of {VALID_INTERVAL_MINUTES}, "
            f"got {interval_minutes!r}"
        )
    ensure_aware(start)
    ensure_aware(end)
    windows = _chunk_windows(
        int(start.timestamp()), int(end.timestamp()), interval_minutes=interval_minutes
    )
    candles: list[Candle] = []
    for chunk_start, chunk_end in windows:
        # No try/except here: a chunk that fails (VenuePayloadError or
        # httpx.HTTPError) must abort the WHOLE fetch, not return
        # whatever earlier chunks already collected — see PARTIAL
        # FAILURE in the module docstring.
        body = await adapter._get(  # noqa: SLF001 - the read-only history funnel this module exists to be
            f"/series/{series}/markets/{ticker}/candlesticks",
            params={
                "start_ts": str(chunk_start),
                "end_ts": str(chunk_end),
                "period_interval": str(interval_minutes),
            },
        )
        raw_candles = _require_envelope_list(
            body, ("candlesticks",), context="kalshi candlesticks"
        )
        candles.extend(_parse_candle(item) for item in raw_candles)
    candles.sort(key=lambda c: c.end_ts)
    return candles


def series_for(market: VenueMarket) -> str:
    """Return the candlestick-endpoint `series` ticker for `market`.

    Kalshi's candlestick path is `/series/{series}/markets/{ticker}/...`,
    where `series` is the SERIES ticker (e.g. `"KXFED"`), not the market
    or event ticker. Derived as `raw["event_ticker"].rsplit("-", 1)[0]`
    (an event ticker is `"{series}-{date/suffix}"`, e.g.
    `"KXFED-26MAR"` -> `"KXFED"`) — the same derivation
    `app/scripts/calibration.py` used ad hoc before this module existed.

    Falls back to `market_id.split("-")[0]` when `event_ticker` is
    absent (`None` or empty) on the payload: Kalshi's own ticker
    convention is `"{series}-{...}-{...}"`, so the market ticker's first
    dash-delimited segment is the series even without an event ticker to
    derive it from.

    Args:
        market: The market to derive a series ticker for.

    Returns:
        str: The series ticker.
    """
    event_ticker = market.raw.get("event_ticker")
    if event_ticker:
        return str(event_ticker).rsplit("-", 1)[0]
    return market.market_id.split("-")[0]


def candle_at_or_before(candles: list[Candle], cutoff_ts: int) -> Candle | None:
    """Return the LAST candle whose `end_ts <= cutoff_ts`, or `None`.

    THIS IS THE NO-LOOK-AHEAD PRIMITIVE the whole replay depends on
    (PLAN.md Risks: "T1 verifies `end_period_ts` is the period END, so
    'last candle with end <= cutoff' has no look-ahead"). `end_ts` is the
    END of a candle's period (module docstring), so a candle that
    satisfies `end_ts <= cutoff_ts` had ALREADY CLOSED at or before
    `cutoff_ts` — everything inside it (its bid/ask close, its trade
    OHLC) was observable at `cutoff_ts` and could have been acted on
    then. A caller that instead picked the FIRST candle with
    `end_ts >= cutoff_ts` would be reading a period that had not
    finished yet at `cutoff_ts` — a look straight through the wall
    clock into the future.

    CONTRACT: correct for `candles` in ANY order, not only the ascending
    order `fetch_candles` returns. This is deliberate, not merely
    tolerated: a caller that merges two fetches, a test fixture, or a
    future script has no way to *prove* it handed this function a sorted
    list, and this primitive's whole reason to exist is that a wrong
    answer here is silent — every Phase-1 P&L number is computed through
    it (PLAN.md Risks). The selection is therefore "the candle with the
    GREATEST `end_ts` among those with `end_ts <= cutoff_ts`", computed
    by scanning every candle and comparing timestamps, never by trusting
    input order or stopping early — an ascending-order-assuming scan that
    breaks at the first `end_ts > cutoff_ts` would silently return the
    wrong candle on an out-of-order list, which is strictly worse than
    the small extra cost of checking every element.

    DUPLICATE `end_ts` TIE-BREAK (documented, not tested for behaviour
    change — T1 retry). The comparison below is a STRICT `>`
    (`candle.end_ts > picked.end_ts`), so once a candle at some `end_ts`
    is picked, a LATER-encountered candle sharing that same `end_ts`
    does not replace it. Two candles claiming to be "the" close of the
    same period is not a shape the venue is documented to send, but if
    it ever did, the result is: the FIRST-encountered candle at the
    maximal qualifying `end_ts` wins. Composed with `fetch_candles`'
    stable `list.sort`, which preserves the venue's wire order among
    equal `end_ts` keys, this means two same-`end_ts` candles resolve to
    "whichever the venue listed first" — a defensible, deterministic
    choice (never "whichever a scan order happens to land on last"), but
    a chosen one, not an inherited one, which is why it is spelled out
    here rather than left to be reverse-engineered from the `>` above.

    Args:
        candles: Candles to search, in any order.
        cutoff_ts: Unix timestamp (seconds) to select at-or-before.

    Returns:
        Candle | None: The candle with the greatest `end_ts <=
            cutoff_ts`, or `None` if `candles` is empty or every
            candle's `end_ts` is strictly after `cutoff_ts` (nothing had
            closed yet at `cutoff_ts` — the no-look-ahead property in
            its purest form: no candle to return beats fabricating one
            that looks ahead).
    """
    picked: Candle | None = None
    for candle in candles:
        if candle.end_ts <= cutoff_ts and (picked is None or candle.end_ts > picked.end_ts):
            picked = candle
    return picked
