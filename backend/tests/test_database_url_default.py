"""The three declarations of the database credentials must agree.

`Settings.database_url`'s default applies in exactly one situation:
nothing configured it. That is the first run — and the first run this
repo documents is `docker compose up`, whose Postgres container is
created with `POSTGRES_USER`/`POSTGRES_PASSWORD` defaulting to
`polymarket`. A default of `postgres:postgres` therefore could not
authenticate against the database the README tells you to start, and
the failure lands on someone who has not yet learned where anything is.

Three files declare these credentials — `app/config.py`,
`.env.example`, `docker-compose.yml` — and drift between them is
invisible until a connection is attempted. The same class of drift
already reopened twice in this repo for env-var coverage, which is why
`tests/test_env_example_coverage.py` exists; this is the same idea
aimed at the one value where the default itself is the trap.

These tests compare userinfo only. Host and database name differ
legitimately (`localhost` for a local process, `postgres` for the
compose network), and pinning those would fail for a correct reason.
"""
import re
from pathlib import Path

import pytest

from app.config import Settings

_REPO_ROOT = Path(__file__).resolve().parents[2]
_USERINFO_RE = re.compile(r"//([^:/@\s]*):([^@/\s]+)@")


def _userinfo(url: str) -> tuple[str, str]:
    """Return `(user, password)` from a connection URL."""
    match = _USERINFO_RE.search(url)
    assert match is not None, f"no userinfo in {url!r}"
    return match.group(1), match.group(2)


def _env_example_database_url() -> str:
    text = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    pytest.fail("DATABASE_URL not found in .env.example")


def _compose_postgres_credentials() -> tuple[str, str]:
    """Read the compose Postgres user/password defaults.

    Both are written `${POSTGRES_USER:-polymarket}`, so the default after
    `:-` is what a `docker compose up` with no `.env` actually creates.
    """
    text = (_REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    user = re.search(r"POSTGRES_USER:\s*\$\{POSTGRES_USER:-([^}]+)\}", text)
    password = re.search(r"POSTGRES_PASSWORD:\s*\$\{POSTGRES_PASSWORD:-([^}]+)\}", text)
    assert user is not None and password is not None, "compose Postgres creds not found"
    return user.group(1), password.group(1)


def test_settings_default_matches_env_example() -> None:
    assert _userinfo(Settings().database_url) == _userinfo(_env_example_database_url())


def test_settings_default_can_authenticate_against_the_compose_database() -> None:
    """The default must match the container the README says to start."""
    assert _userinfo(Settings().database_url) == _compose_postgres_credentials()


def test_the_default_is_not_the_postgres_superuser_convention() -> None:
    """Regression pin, named so the reason survives.

    `postgres:postgres` is the reflexive default and was wrong here for a
    specific reason: the compose Postgres is created with a `polymarket`
    superuser, so no `postgres` role exists to authenticate as. If this
    ever reads `postgres` again, the first-run trap is back.
    """
    user, _ = _userinfo(Settings().database_url)

    assert user == "polymarket"
