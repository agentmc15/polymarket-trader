"""`app.logging_config` — the structured logging the app actually installs.

Every order event in `app/execution/router.py` is
`logger.info("order", extra={...})`, and the single worst state the
system can reach announces itself as

    logger.error("order", extra={..., "naked_exposure": True})

Under uvicorn's and Celery's default logging those `extra` fields reach
no formatter at all: `%(message)s` is the whole record, so that alarm
printed the bare word `order` on stderr and every field was discarded.
There was no `basicConfig`, no `dictConfig` and no formatter anywhere in
`app/` outside three `app/scripts/` entry points.

These tests hold two properties: the fields SURVIVE, and a secret NEVER
does (GUARDRAILS.md §1.3). The second is not hypothetical —
`app.venues.base.VenuePayloadError` carries a `raw` attribute holding a
whole venue response body, and a formatter that reached for `repr()` or
`vars()` on an exception would flush that body into the log.
"""
import json
import logging

import pytest

from app.logging_config import (
    LOGGING_CONFIG,
    JsonLogFormatter,
    configure_logging,
)
from app.venues.base import VenuePayloadError

#: A value that must never appear in a log line, in any test below.
SECRET = "0xdeadbeefPRIVATEKEYdeadbeef"


def render(
    message: str = "order",
    *,
    level: int = logging.INFO,
    exc_info: object = None,
    **fields: object,
) -> dict[str, object]:
    """Format one record through `JsonLogFormatter` and parse it back.

    Args:
        message: The log message.
        level: Log level.
        exc_info: Optional `exc_info` triple/exception.
        **fields: The `extra` fields.

    Returns:
        dict[str, object]: The decoded JSON line.
    """
    record = logging.LogRecord(
        name="app.execution.router",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=exc_info,  # type: ignore[arg-type]
    )
    for key, value in fields.items():
        setattr(record, key, value)
    return json.loads(JsonLogFormatter().format(record))


def test_extra_fields_survive_the_formatter() -> None:
    """The naked-leg alarm must carry its fields, not just the word "order"."""
    line = render(
        "order",
        level=logging.ERROR,
        event="unwind_failed",
        mode="paper",
        venue="polymarket",
        market_id="PM-1",
        outcome="YES",
        client_order_id="intent:0:0",
        size=10.0,
        price=0.5,
        reason="no_bid",
        naked_exposure=True,
    )

    assert line["message"] == "order"
    assert line["level"] == "ERROR"
    assert line["logger"] == "app.execution.router"
    assert line["event"] == "unwind_failed"
    assert line["naked_exposure"] is True
    assert line["client_order_id"] == "intent:0:0"
    assert line["size"] == 10.0
    assert line["reason"] == "no_bid"


def test_nested_structures_and_unknown_objects_are_rendered_safely() -> None:
    """Lists and dicts survive; anything else becomes its class name."""

    class Opaque:
        """Something with a `__repr__` nobody vetted."""

        def __repr__(self) -> str:
            return f"Opaque(secret={SECRET})"

    line = render(
        "order",
        legs=[{"venue": "kalshi", "size": 3.0}],
        adapter=Opaque(),
    )

    assert line["legs"] == [{"venue": "kalshi", "size": 3.0}]
    assert line["adapter"] == "<Opaque>"
    assert SECRET not in json.dumps(line)


def test_an_exception_in_extra_never_serializes_its_raw_payload() -> None:
    """`VenuePayloadError.raw` holds a whole response body. It must not leak.

    The type allow-list is what makes this structural: an exception is
    not a scalar, a list or a dict, so it renders as its class name and
    no attribute of it is ever reached.
    """
    error = VenuePayloadError(
        "could not parse order response",
        raw={"api_key": SECRET, "body": f"authorization: Bearer {SECRET}"},
    )

    line = render("order", event="place_failed", error=error)
    encoded = json.dumps(line)

    assert line["error"] == "<VenuePayloadError>"
    assert SECRET not in encoded
    assert "authorization" not in encoded


def test_a_traceback_carries_the_message_but_not_the_raw_payload() -> None:
    """`logger.exception()` renders standard traceback text, nothing more."""
    try:
        raise VenuePayloadError("could not parse order response", raw={"k": SECRET})
    except VenuePayloadError as exc:
        line = render(
            "order",
            level=logging.ERROR,
            exc_info=(type(exc), exc, exc.__traceback__),
            event="place_failed",
        )

    assert "could not parse order response" in str(line["exception"])
    assert SECRET not in json.dumps(line)


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "polymarket_private_key",
        "kalshi_api_secret",
        "authorization",
        "password",
        "passphrase",
    ],
)
def test_credential_shaped_keys_are_redacted(key: str) -> None:
    """A caller that passes a credential by name gets `***`, not the value."""
    line = render("order", **{key: SECRET})

    assert line[key] == "***"
    assert SECRET not in json.dumps(line)


def test_token_id_is_not_redacted() -> None:
    """A CLOB token id is public market data the order logs need.

    The redaction list is deliberately narrow — matching `"token"` would
    blind every order line for no security benefit.
    """
    line = render("order", token_id="PM-1-yes")

    assert line["token_id"] == "PM-1-yes"


def test_configure_logging_installs_a_handler_that_emits_extra(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The config the app installs must actually reach `app.*` loggers.

    `app/main.py` and `app/tasks/__init__.py` both call this; if the
    installed handler did not carry `extra`, every property above would
    be true of a formatter nobody uses.
    """
    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        configure_logging(force=True)
        logging.getLogger("app.execution.router").error(
            "order", extra={"event": "unwind_failed", "naked_exposure": True}
        )
        for handler in logging.getLogger().handlers:
            handler.flush()
        emitted = capsys.readouterr().err.strip().splitlines()[-1]
    finally:
        root.handlers[:] = saved

    line = json.loads(emitted)
    assert line["event"] == "unwind_failed"
    assert line["naked_exposure"] is True


def test_app_records_propagate_so_other_handlers_still_see_them() -> None:
    """`app` must not be a dead end.

    A non-propagating `app` logger would be invisible to any root handler
    an operator (or a test harness) attaches — including the capture
    handler the rest of this suite asserts against.
    """
    assert "handlers" not in LOGGING_CONFIG["loggers"]["app"]
    assert LOGGING_CONFIG["loggers"]["app"].get("propagate", True) is True
    assert LOGGING_CONFIG["root"]["handlers"] == ["json"]
