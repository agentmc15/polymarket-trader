"""`alembic/env.py` must hand an ASYNC url to the online migration path.

This defect could not be seen by the one command GUARDRAILS.md allows.
`alembic upgrade head --sql` runs `run_migrations_offline`, which
configures a URL and never builds an engine, so it passes with any
driver string at all. The online path -- what `alembic upgrade head`
actually uses -- calls `async_engine_from_config`, and SQLAlchemy's
asyncio extension refuses a sync driver outright.

`env.py` stripped `+asyncpg` at MODULE scope, so the URL reaching the
online path was a bare `postgresql://`. That resolves to psycopg2,
which is not in `requirements.txt` (only asyncpg is), and even with it
installed `create_async_engine` raises `InvalidRequestError: The
asyncio extension requires an async driver`. Verified against a real
TimescaleDB container: the stripped URL raised, the async URL connected.

So the safety rule and the bug pointed the same way -- the only
sanctioned check was the only check that could not fail. These tests
read the source instead of running a migration, so they hold without a
database and without violating that rule.
"""
import ast
from pathlib import Path

import pytest

_ENV_PY = Path(__file__).resolve().parents[1] / "alembic" / "env.py"


def _module() -> ast.Module:
    return ast.parse(_ENV_PY.read_text(encoding="utf-8"))


def _strips_asyncpg(node: ast.AST) -> bool:
    """True if `node` contains a `.replace("+asyncpg", ...)` call."""
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "replace"
            and sub.args
            and isinstance(sub.args[0], ast.Constant)
            and sub.args[0].value == "+asyncpg"
        ):
            return True
    return False


def _module_level_url_assignment() -> ast.Call:
    """The module-scope `config.set_main_option("sqlalchemy.url", ...)` call."""
    for node in _module().body:
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "set_main_option"
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and call.args[0].value == "sqlalchemy.url"
        ):
            return call
    pytest.fail("no module-level config.set_main_option('sqlalchemy.url', ...) in env.py")


def test_module_scope_url_is_not_stripped_of_its_async_driver() -> None:
    """The regression itself.

    Stripping here breaks `alembic upgrade head` against Postgres
    entirely, while leaving `--sql` green.
    """
    call = _module_level_url_assignment()

    assert not _strips_asyncpg(call.args[1]), (
        "env.py strips '+asyncpg' at module scope; the online migration path "
        "feeds this URL to async_engine_from_config, which refuses a sync driver"
    )


def test_offline_mode_is_where_the_driver_is_stripped() -> None:
    """Stripping is correct in offline mode and must stay there.

    Offline emits SQL and never connects, so a rendered
    `postgresql+asyncpg://` would just be misleading. This asserts the
    fix relocated the strip rather than deleting it.
    """
    offline = next(
        (
            node
            for node in _module().body
            if isinstance(node, ast.FunctionDef) and node.name == "run_migrations_offline"
        ),
        None,
    )

    assert offline is not None, "run_migrations_offline missing from env.py"
    assert _strips_asyncpg(offline), (
        "run_migrations_offline no longer strips '+asyncpg'; offline SQL would "
        "render a driver it never uses"
    )
