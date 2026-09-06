"""Timezone-aware datetime helpers.

All datetimes in this codebase are aware UTC. A naive datetime anywhere in the
backtesting or execution path is a defect, not an environment quirk (see
GUARDRAILS.md and PLAN.md R9).
"""
from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return the current time as an aware UTC datetime.

    Returns:
        datetime: Current time with `tzinfo=UTC`.
    """
    return datetime.now(UTC)


def ensure_aware(dt: datetime) -> datetime:
    """Validate that a datetime carries timezone information.

    Args:
        dt: The datetime to check.

    Returns:
        datetime: The same datetime, unchanged.

    Raises:
        TypeError: If `dt` is not a `datetime` instance. Note that
            `datetime.date` is the subtle case: every `datetime` is also a
            `date` (`isinstance(datetime_obj, date)` is `True`), but a plain
            `date` is not a `datetime` (`isinstance(date_obj, datetime)` is
            `False`), so checking `isinstance(dt, datetime)` correctly
            rejects a bare `date` while accepting real datetimes.
        ValueError: If `dt` is naive (`dt.tzinfo is None`).
    """
    if not isinstance(dt, datetime):
        raise TypeError(f"expected datetime, got {type(dt).__name__}")
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt
