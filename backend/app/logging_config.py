"""The logging configuration the application actually installs (T14 remediation).

WHY THIS FILE EXISTS. Every order event in `app/execution/router.py` is
emitted as `logger.info("order", extra={...})` — the event name, the
mode, the venue, the client order id, the size, the price. The single
most important line the system can emit,

    logger.error("order", extra={..., "naked_exposure": True})

says "we are holding a directional position the strategy never asked for
and could not close". Under uvicorn's and Celery's default logging, an
`extra` dict reaches no formatter at all: `%(message)s` is the whole
record, so that alarm printed the bare word `order` on stderr and
threw every field away. There was no `basicConfig`, no `dictConfig` and
no formatter anywhere in `app/` outside three `app/scripts/` entry
points. This module is the missing piece, and `app/main.py` and
`app/tasks/__init__.py` install it.

DEPENDENCY-LIGHT ON PURPOSE. `requirements.txt` carries no `structlog`
and no JSON-log package; `JsonLogFormatter` below is ~40 lines of stdlib
and adds nothing to install.

NEVER A SECRET (GUARDRAILS.md §1.3). Two rules make that structural
rather than aspirational:

1. **Values are allow-listed by TYPE, not filtered by name.** Only
   `str`/`int`/`float`/`bool`/`None` and lists/dicts of those are
   serialized. Anything else becomes `"<ClassName>"`. In particular an
   EXCEPTION object is never serialized by value: `VenuePayloadError`
   carries a `raw` attribute holding a whole venue response body, and a
   formatter that reached for `repr()` or `vars()` would flush that body
   — potentially including an authenticated payload — into the log. The
   type allow-list means no attribute of any exception is ever reached.
2. **A small set of key names is redacted outright**, so a caller that
   passes `extra={"api_key": ...}` gets `"***"` rather than the value.
   `token_id` and friends are deliberately NOT matched: a CLOB token id
   is public market data and losing it would blind the order logs.

`exc_info` is rendered by `logging.Formatter.formatException`, i.e. the
standard traceback text, which calls `str()` on the exception — the same
message that already reached stderr before this module existed. It never
touches exception attributes.
"""
import copy
import json
import logging
import logging.config
from typing import Any

#: Attributes `logging.LogRecord` sets itself. Everything on a record
#: that is NOT in here came from a caller's `extra=` and is exactly what
#: this formatter exists to emit.
_RESERVED_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

#: Substrings that make an `extra` KEY sensitive. Matched
#: case-insensitively against the key, and its value is replaced with
#: `_REDACTED` before it is ever encoded. Deliberately narrow: `"token"`
#: alone is NOT here, because `token_id` is a public CLOB identifier that
#: the order logs need.
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "passphrase",
    "password",
    "private_key",
    "secret",
    "signature",
)

#: What a redacted value is replaced with.
_REDACTED = "***"

#: How deep a nested list/dict from `extra` is walked before it is
#: summarized. Bounds the work a pathological payload can cause.
_MAX_DEPTH = 4


def _is_sensitive(key: str) -> bool:
    """Return `True` if an `extra` key must be redacted.

    Args:
        key: The `extra` field name.

    Returns:
        bool: `True` if the key looks like a credential.
    """
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _safe(value: Any, depth: int = 0) -> Any:
    """Coerce one `extra` value into something JSON-safe and secret-free.

    Scalars pass through; lists and dicts are walked to `_MAX_DEPTH`;
    EVERYTHING else — an exception, an adapter, a venue payload object,
    a dataclass — becomes `"<ClassName>"`. That last rule is the
    security-relevant one: it is why no attribute of a
    `VenuePayloadError` (which holds a whole raw response body in `raw`)
    can reach a log line through this formatter, no matter how it was
    passed in.

    Args:
        value: The value from a record's `extra`.
        depth: Current recursion depth.

    Returns:
        Any: A JSON-encodable value.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if depth >= _MAX_DEPTH:
        return f"<{type(value).__name__}>"
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe(item, depth + 1) for item in value]
    if isinstance(value, dict):
        return {
            str(key): (
                _REDACTED if _is_sensitive(str(key)) else _safe(item, depth + 1)
            )
            for key, item in value.items()
        }
    return f"<{type(value).__name__}>"


class JsonLogFormatter(logging.Formatter):
    """Render a `LogRecord` — INCLUDING its `extra` fields — as one JSON line.

    The `extra` fields are the entire point: without them
    `logger.error("order", extra={"naked_exposure": True, ...})` is the
    word `order`.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Return the record as a single-line JSON object.

        Args:
            record: The record to render.

        Returns:
            str: One JSON object, no newline.
        """
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key.startswith("_"):
                continue
            if key in payload:
                # A caller's own field never overwrites the frame above.
                key = f"extra_{key}"
            payload[key] = _REDACTED if _is_sensitive(key) else _safe(value)
        if record.exc_info:
            # The standard traceback text (which calls `str()` on the
            # exception), never the exception object or its attributes.
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=lambda item: f"<{type(item).__name__}>")


#: The `logging.config.dictConfig` this application installs.
#:
#: `disable_existing_loggers` is `False` so import order cannot silence a
#: module logger that was created first. The handler lives on the ROOT
#: logger and `"app"` sets only its level, so `app.*` records PROPAGATE:
#: uvicorn's default config configures the `uvicorn*` loggers and leaves
#: root alone, Celery is told not to hijack root
#: (`worker_hijack_root_logger=False` in `app/tasks/__init__.py`), and
#: anything else that attaches a root handler — a test harness's capture
#: handler, an operator's own aggregator — still sees every order event.
#: A non-propagating `app` logger would have been invisible to all of
#: them.
LOGGING_CONFIG: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {"()": "app.logging_config.JsonLogFormatter"},
    },
    "handlers": {
        "json": {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "stream": "ext://sys.stderr",
        },
    },
    "loggers": {
        "app": {"level": "INFO"},
    },
    "root": {"handlers": ["json"], "level": "INFO"},
}

#: Set once `configure_logging()` has installed `LOGGING_CONFIG`, so the
#: several entry points that call it (the FastAPI app, the Celery app,
#: a script) cannot stack duplicate handlers on the same logger.
_configured = False


def configure_logging(*, level: str | None = None, force: bool = False) -> None:
    """Install `LOGGING_CONFIG`, once per process.

    Called from `app.main.create_application()` and from
    `app.tasks.__init__` (with `worker_hijack_root_logger` disabled), so
    both the API process and the Celery worker emit the same structured
    lines. Safe to call more than once; the second call is a no-op unless
    `force`.

    Args:
        level: Override the level for the `app` logger and the root
            logger (e.g. `"DEBUG"`). `None` keeps `INFO`.
        force: Re-install even if this process already configured
            logging. Tests use this; application code does not.
    """
    global _configured
    if _configured and not force:
        return
    config = copy.deepcopy(LOGGING_CONFIG)
    if level is not None:
        config["loggers"]["app"]["level"] = level
        config["root"]["level"] = level
    logging.config.dictConfig(config)
    _configured = True
