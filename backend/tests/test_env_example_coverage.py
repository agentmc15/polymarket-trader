"""`.env.example` must document every real `Settings` alias (T40).

NOTES.md records this exact bug class being fixed once for `MIN_TRADE_USD`
(T10) and reopened by nearly every field added since -- a `Settings`
field can carry a real alias, a real default, and be read by production
code, and still be invisible to an operator who has never opened
`config.py`, because nothing failed when a new field skipped
`.env.example`. This test is that check: it enumerates
`Settings.model_fields` directly, so it cannot itself drift out of date
the way a hand-maintained list would, and it fails the moment a new
field's alias is missing from the template file.

The reverse direction matters too: `.env.example` legitimately contains a
handful of names that are NOT `Settings` fields at all --
`POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB` are consumed by the
`postgres` container (`docker-compose.yml`), not by the Python process,
and `VITE_API_URL` is a Vite/frontend build-time variable, not a
`pydantic-settings` field. Those are named explicitly in
`_NON_SETTINGS_NAMES` below rather than silently tolerated, so a
genuinely stray or misspelled name still fails the test.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings

#: Repo root, resolved from this test file's own location so the check
#: works regardless of the process's current working directory (same
#: convention as `tests/test_fences.py::APP_ROOT`).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"

#: Names `.env.example` legitimately carries that are NOT `Settings`
#: fields -- see module docstring. Adding a name here should be rare and
#: deliberate; it is not an escape hatch for a `Settings` field that is
#: merely inconvenient to document.
_NON_SETTINGS_NAMES = frozenset(
    {
        "POSTGRES_USER",  # postgres container (docker-compose.yml), not Settings
        "POSTGRES_PASSWORD",  # ditto
        "POSTGRES_DB",  # ditto
        "VITE_API_URL",  # frontend build-time var (Vite), not Settings
    }
)

#: Every `VARNAME=` (commented or not) anchored at line-start in
#: `.env.example`. Anchoring matters: several comments embed an example
#: like `{"crypto": 0.08}` or prose mentioning a name mid-sentence, and
#: those must never be mistaken for a documented assignment.
_ASSIGNMENT_RE = re.compile(r"(?m)^#?\s*([A-Z][A-Z0-9_]*)=")


def _documented_names() -> set[str]:
    """Return every name `.env.example` documents, commented or not."""
    text = _ENV_EXAMPLE.read_text()
    return set(_ASSIGNMENT_RE.findall(text))


def _settings_aliases() -> set[str]:
    """Return the env-var spelling of every `Settings` field.

    Mirrors `pydantic-settings`'s own resolution: an explicit `alias=` if
    the field declares one, else the field name -- `case_sensitive=False`
    in `Settings.model_config` means the field name in any case is what
    the environment loader matches, so upper-casing it here is exactly
    what `Settings` would accept.
    """
    return {
        field.alias or name.upper() for name, field in Settings.model_fields.items()
    }


def test_env_example_exists() -> None:
    """Sanity check the path resolution above before trusting its result."""
    assert _ENV_EXAMPLE.is_file(), _ENV_EXAMPLE


def test_every_settings_alias_is_documented() -> None:
    """Every real `Settings` field must have a line an operator can find."""
    missing = sorted(_settings_aliases() - _documented_names())

    assert not missing, (
        f"{missing} are real Settings aliases with no line in "
        ".env.example -- add them (commented, with their real default) "
        "near the fields they belong with. See config.py for the default "
        "and the surrounding comment for what reads it."
    )


def test_no_undocumented_non_settings_names() -> None:
    """A name in the file must be a real `Settings` alias or an explicit exemption.

    Catches the opposite drift: a typo'd/stray env var name, or a
    genuinely new non-Settings name that should be added to
    `_NON_SETTINGS_NAMES` deliberately rather than silently.
    """
    unexplained = sorted(
        _documented_names() - _settings_aliases() - _NON_SETTINGS_NAMES
    )

    assert not unexplained, (
        f"{unexplained} appear in .env.example but are neither a real "
        "Settings alias nor listed in _NON_SETTINGS_NAMES -- fix the "
        "typo, or if it is a genuine non-Settings name (consumed by "
        "docker-compose.yml or the frontend build), add it to "
        "_NON_SETTINGS_NAMES with a comment saying what consumes it."
    )
