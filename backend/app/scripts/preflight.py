#!/usr/bin/env python
"""Preflight readiness check — run BEFORE starting the stack.

Usage:
    python3 -m app.scripts.preflight

Exits `0` when everything needed for the CONFIGURED mode (`TRADING_MODE`)
is present; non-zero otherwise. Prints a grouped, pass/warn/fail report to
stdout either way — the report is the product, not the exit code alone
(see module rationale below).

WHY THIS EXISTS (T45). A deployment audit found that 44 of `Settings`'
56 fields never reached the Docker container at all — `docker-compose.yml`
had no `env_file:`, so `.env.example`'s own "copy this to `.env`"
instruction was false, and BOTH Kalshi credentials were among the fields
silently dropped. Nothing failed loudly; the container just ran on
compiled-in defaults. That gap was invisible until someone read the
compose file against the settings model by hand. This script is the
mechanical version of that reading: it inspects the SAME `Settings`
instance the application would construct, plus database/broker
reachability, and reports what it finds — so the first time anyone runs
this system is not also the first time anyone discovers the next hole
like that one.

WHAT THIS NEVER DOES (GUARDRAILS.md §1.3, §1.4):
  - Never contacts a venue. Polymarket, Kalshi and any Polygon RPC are
    OFF LIMITS here, same as in tests. A green run says NOTHING about
    whether a credential actually authenticates — see the "NOT checked"
    section every report prints.
  - Never prints, logs, or returns a secret VALUE. Credential checks
    report presence and coarse shape only (`len()`, a PEM-prefix check,
    a "0x"-prefix check) — never the string itself. `test_preflight.py`
    has a dedicated test asserting a recognizable fake secret never
    appears anywhere in rendered output.
  - Never runs `alembic upgrade head`. The database check compares the
    `alembic_version` table's recorded revision against the local
    migration files' head (`ScriptDirectory.get_heads()`, itself no
    more of a "venue" than reading a `.py` file) and reports drift; it
    is read-only end to end.

FAIL vs. WARN, the actual design decision here: FAIL means "the
configured mode cannot work" (no database, no credentials the
configured mode needs). WARN means "this works, but the configuration
looks like it does not mean what it says" (a kill switch left armed, a
credential set that the code silently ignores because a sibling
setting isn't also set, an env var that looks like a real `Settings`
name but is a near-miss typo `extra="ignore"` will drop without
complaint). Only FAIL affects the exit code — a tool that fails on
warnings gets ignored, and the reverse mistake (warning on a real
failure) gets trusted wrongly, so keeping that split honest is most of
this file's value.

Every connectivity check (database, Redis/Celery broker) is injected
into `build_report` as an already-computed result (`DatabaseCheck`/
`BrokerCheck`), not as a callable this module invokes internally.
`backend/tests/test_preflight.py` builds those results by hand — no
test here ever needs a live Postgres or Redis.
"""
from __future__ import annotations

import asyncio
import difflib
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings, get_settings
from app.execution.fences import LIVE_TRADING_CONFIRMATION_PHRASE

Status = Literal["pass", "warn", "fail"]

_STATUS_RANK: dict[Status, int] = {"pass": 0, "warn": 1, "fail": 2}
_STATUS_LABEL: dict[Status, str] = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}

#: `Settings` names that legitimately appear in the environment but are
#: NOT `Settings` fields — the postgres container reads these three
#: directly (`docker-compose.yml`), and the frontend build reads the
#: fourth. Mirrors `tests/test_env_example_coverage.py::_NON_SETTINGS_NAMES`
#: (kept as its own copy rather than an import — this module has no other
#: reason to depend on `tests/`, and the set is small and stable).
_NON_SETTINGS_ENV_NAMES = frozenset(
    {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "VITE_API_URL"}
)

#: Similarity cutoff for the near-miss env-var-name check below
#: (`difflib.get_close_matches`). Chosen empirically against this
#: repo's own 56 real aliases plus a batch of ordinary shell/OS
#: variables (`PATH`, `HOME`, `LANG`, `PYTHONPATH`, `CI`, ...): `0.75`
#: catches the real, previously-documented gotcha this check exists for
#: (`TRADING_KILL_SWITCH_PATH`, a plausible typo of `KILL_SWITCH_PATH`
#: — see README.md "Money safety") while producing zero false positives
#: against that batch. `0.8` was tried first and MISSED that exact
#: gotcha (ratio 0.80, just under a 0.82 cutoff) — this constant is
#: tuned to the case it is for, not picked round.
_NEAR_MISS_CUTOFF = 0.75

#: Every string substring `FORBIDDEN_PATH_SUBSTRINGS`-style venue path a
#: connection URL might carry a password next to. Matches `user:password@`
#: in a `postgresql+asyncpg://...`/`redis://...` URL and masks only the
#: password half — the host/port/db name are not secret and are useful
#: in a report; GUARDRAILS.md §1.3 does not name `DATABASE_URL`/
#: `REDIS_URL` explicitly, but they can carry a real password and this
#: script prints URLs in its report, so they are redacted defensively.
#: The username half is `*`, not `+`, deliberately: `redis://:password@host`
#: -- no username at all -- is the STANDARD Redis URL form for password-only
#: AUTH, and is what `CELERY_BROKER_URL` looks like on essentially every
#: managed Redis. Requiring a non-empty username silently left that exact
#: case unmasked, which is the most likely real-world leak, not the least.
_URL_PASSWORD_RE = re.compile(r"//([^:/@\s]*):([^@/\s]+)@")


def _redact_url(url: str) -> str:
    """Mask a connection URL's password, if it has one, before display.

    Args:
        url: A `postgresql+asyncpg://`/`redis://`-shaped connection URL.

    Returns:
        str: `url` with `user:password@` rewritten to `user:***@`.
            Unchanged if the URL carries no userinfo password.
    """
    return _URL_PASSWORD_RE.sub(lambda m: f"//{m.group(1)}:***@", url)


@dataclass(frozen=True)
class Check:
    """One pass/warn/fail line in the report.

    Attributes:
        status: `"pass"`, `"warn"`, or `"fail"`.
        message: The full line, including any remediation — this is
            read by an operator, so it says what to do about a
            `"fail"`/`"warn"`, not just that one occurred.
    """

    status: Status
    message: str


@dataclass(frozen=True)
class CheckGroup:
    """One concern's worth of `Check`s (PLAN.md-style "group by concern").

    Attributes:
        name: Section heading.
        checks: The checks in this section, in report order.
    """

    name: str
    checks: list[Check] = field(default_factory=list)

    @property
    def status(self) -> Status:
        """Worst status among this group's checks; `"pass"` if empty."""
        if not self.checks:
            return "pass"
        return max((c.status for c in self.checks), key=_STATUS_RANK.__getitem__)


@dataclass(frozen=True)
class DatabaseCheck:
    """Result of a (possibly faked) database connectivity + schema probe.

    Attributes:
        reachable: Whether a connection could be opened at all.
        display_url: The connection URL, password-redacted, for display.
        error: Connection failure detail, when `reachable` is `False`.
        current_revision: The `alembic_version` table's `version_num`,
            when reachable and the table exists and holds a row.
        schema_error: Set instead of `current_revision` when reachable
            but the schema could not be read (e.g. no `alembic_version`
            table at all — an unmigrated database).
    """

    reachable: bool
    display_url: str
    error: str | None = None
    current_revision: str | None = None
    schema_error: str | None = None


@dataclass(frozen=True)
class BrokerCheck:
    """Result of a (possibly faked) Redis/Celery broker reachability probe.

    Attributes:
        label: Which `Settings` field this came from, e.g.
            `"CELERY_BROKER_URL"` — several beats depend on this
            specifically, not on `REDIS_URL` (see `_inert_settings_group`).
        display_url: The URL, password-redacted, for display.
        reachable: Whether a `PING` succeeded.
        error: Failure detail, when `reachable` is `False`.
    """

    label: str
    display_url: str
    reachable: bool
    error: str | None = None


@dataclass(frozen=True)
class PreflightReport:
    """The full report: every group, plus what was deliberately not checked."""

    groups: list[CheckGroup]
    not_checked: list[str]

    @property
    def status(self) -> Status:
        """Worst status across every group; `"pass"` if there are none."""
        if not self.groups:
            return "pass"
        return max((g.status for g in self.groups), key=_STATUS_RANK.__getitem__)

    @property
    def exit_code(self) -> int:
        """`1` if anything FAILs, else `0` — WARN never affects this.

        A tool that fails the run on a warning gets disabled at the
        first false alarm; a tool that only warns on a real failure
        gets trusted when it should not be. Keeping FAIL the only exit-
        code trigger is what keeps both promises honest.
        """
        return 1 if self.status == "fail" else 0

    def render(self) -> str:
        """Render the full, grouped, pass/warn/fail report as text."""
        lines: list[str] = ["Preflight check (app.scripts.preflight)", "=" * 78]
        n_pass = n_warn = n_fail = 0
        for group in self.groups:
            lines.append("")
            heading = f"-- {group.name} "
            lines.append(heading + "-" * max(4, 78 - len(heading)))
            for check in group.checks:
                lines.append(f"[{_STATUS_LABEL[check.status]}] {check.message}")
                if check.status == "pass":
                    n_pass += 1
                elif check.status == "warn":
                    n_warn += 1
                else:
                    n_fail += 1
        lines.append("")
        lines.append("-- NOT checked by this tool " + "-" * 50)
        for item in self.not_checked:
            lines.append(f"  - {item}")
        lines.append("")
        lines.append("=" * 78)
        lines.append(
            f"SUMMARY: {n_pass} passed, {n_warn} warning(s), {n_fail} failed -- "
            f"overall {_STATUS_LABEL[self.status]} (exit code {self.exit_code})"
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Group builders. Each is a pure function of already-resolved inputs --
# no network, no filesystem beyond `Path.exists()` on the kill-switch path
# (GUARDRAILS.md: checking existence is not a venue).
# ---------------------------------------------------------------------------


def _trading_mode_group(settings_obj: Settings) -> CheckGroup:
    """Trading mode, its confirmation phrase, and the kill switch.

    Ends with an explicit DECISION line: whether this process would
    place a real order RIGHT NOW, computed with the identical logic
    `app.execution.fences.assert_live_allowed` uses, so an operator
    never has to derive it themselves from the three pieces above it.
    """
    checks: list[Check] = [Check("pass", f"TRADING_MODE={settings_obj.trading_mode!r}")]

    mode_live = settings_obj.trading_mode == "live"
    confirmed = (
        settings_obj.live_trading_confirmation == LIVE_TRADING_CONFIRMATION_PHRASE
    )
    switch_path = settings_obj.kill_switch_path
    switch_present = Path(switch_path).exists()

    if mode_live and confirmed:
        checks.append(Check("pass", "LIVE_TRADING_CONFIRMATION matches the required phrase."))
    elif mode_live and not confirmed:
        checks.append(
            Check(
                "fail",
                "LIVE_TRADING_CONFIRMATION does not match the required phrase "
                "('I_UNDERSTAND_REAL_MONEY'); live order placement is disabled. "
                "TRADING_MODE=live alone is never enough -- set "
                "LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_REAL_MONEY too.",
            )
        )
    elif not mode_live and confirmed:
        checks.append(
            Check(
                "warn",
                "LIVE_TRADING_CONFIRMATION is set to the live-trading phrase, but "
                "TRADING_MODE=paper -- it has no effect until TRADING_MODE=live "
                "(a setting that is set but currently inert).",
            )
        )
    else:
        checks.append(Check("pass", "LIVE_TRADING_CONFIRMATION not set (fine for paper mode)."))

    if switch_present and mode_live and confirmed:
        checks.append(
            Check(
                "warn",
                f"Kill switch file present at {switch_path!r} -- this is the ONLY "
                "thing currently preventing live order placement (TRADING_MODE "
                "and LIVE_TRADING_CONFIRMATION are both correctly armed). Delete "
                "the file to re-arm live trading, if that is what you intend.",
            )
        )
    elif switch_present:
        checks.append(
            Check(
                "warn",
                f"Kill switch file present at {switch_path!r} -- has no effect "
                f"while trading_mode={settings_obj.trading_mode!r} (paper mode "
                "never places real orders regardless).",
            )
        )
    else:
        checks.append(Check("pass", f"No kill-switch file at {switch_path!r}."))

    would_place_real_orders = mode_live and confirmed and not switch_present
    checks.append(
        Check(
            "pass",
            "DECISION: this process WOULD place real orders right now."
            if would_place_real_orders
            else "DECISION: this process would NOT place real orders right now.",
        )
    )
    return CheckGroup("Trading mode & fences", checks)


def _presence_checks(
    alias: str,
    value: str,
    *,
    pem_expected: bool = False,
    warn_if_0x_prefixed: bool = False,
) -> list[Check]:
    """Presence-and-shape-only report for one credential-shaped field.

    NEVER returns or logs `value` itself -- only its length and a couple
    of coarse, boolean shape signals (a PEM prefix, a `0x` prefix).
    Whether an empty value is itself a FAIL/WARN is a separate, per-venue
    decision the caller makes (Polymarket's API-credential trio and
    Kalshi's key/PEM pair are each "both or neither", not independently
    required -- see `_credentials_group`), so this never returns
    fail/warn for a merely-absent value on its own.

    Args:
        alias: The `Settings` field's env-var alias, for display.
        value: The actual secret/credential string -- read only to
            measure it, never printed.
        pem_expected: If `True`, `value` is expected to be a PEM block;
            reports byte length and flags a non-PEM shape.
        warn_if_0x_prefixed: If `True`, warns when `value` still carries
            a `0x` prefix (`.env.example` documents
            `POLYMARKET_PRIVATE_KEY` as "without 0x prefix").

    Returns:
        list[Check]: One presence/shape line, plus an extra WARN line
            if the shape looks wrong. Never empty.
    """
    if not value:
        return [Check("pass", f"{alias}: not set")]
    if pem_expected:
        looks_pem = value.strip().startswith("-----BEGIN") and "PRIVATE KEY-----" in value
        n_bytes = len(value.encode("utf-8"))
        if looks_pem:
            return [Check("pass", f"{alias}: set (PEM, {n_bytes} bytes)")]
        return [
            Check("pass", f"{alias}: set ({n_bytes} bytes)"),
            Check(
                "warn",
                f"{alias} is set but does not look like a PEM block (expected to "
                "start with '-----BEGIN' and contain 'PRIVATE KEY-----'); signing "
                "will fail with a configuration error at first use, not silently.",
            ),
        ]
    checks: list[Check] = [Check("pass", f"{alias}: set ({len(value)} chars)")]
    if warn_if_0x_prefixed and value.lower().startswith("0x"):
        checks.append(
            Check(
                "warn",
                f"{alias} appears to still carry a '0x' prefix; .env.example "
                "documents this key WITHOUT the 0x prefix -- double-check it was "
                "stripped when copied from a wallet export.",
            )
        )
    return checks


def _missing_required_check(alias_or_label: str, *, mode: str, detail: str) -> Check:
    """FAIL in live mode, WARN in paper mode, for a genuinely-missing
    required credential (never for a merely-optional one)."""
    if mode == "live":
        return Check("fail", f"{alias_or_label}: MISSING -- {detail}")
    return Check(
        "warn",
        f"{alias_or_label}: MISSING -- fine for paper mode; {detail}",
    )


def _credentials_group(settings_obj: Settings) -> CheckGroup:
    """Presence and shape of every venue credential -- never a value.

    Two subtleties are checked deliberately, both traced from the real
    code that reads these fields rather than assumed from field names:

    - Polymarket's `POLYMARKET_API_KEY`/`_SECRET`/`_PASSPHRASE` are only
      used together (`app/services/polymarket/client.py::
      ClobClientWrapper.initialize`: `if api_key and api_secret and
      api_passphrase: ... else: derive`). Setting one or two of the
      three is silently discarded in favour of `create_or_derive_api_
      creds()` -- a real "looks configured, does nothing" case, flagged
      here as its own warning rather than as three independent
      "missing" lines.
    - Kalshi's `KALSHI_API_KEY_ID`/`KALSHI_PRIVATE_KEY_PEM` are likewise
      required TOGETHER (`app/venues/kalshi/adapter.py::_signing_key`:
      `if not (key_id and pem.strip()): return None`) -- one alone
      signs nothing.

    `POLYMARKET_PRIVATE_KEY` is the one Polymarket field that is
    unconditionally required (`ClobClientWrapper.initialize` raises
    `ValueError` if it is empty, independent of the API-credential
    trio), so it alone gets a plain required/missing check.
    """
    mode = settings_obj.trading_mode
    checks: list[Check] = []

    private_key = settings_obj.polymarket_private_key.get_secret_value()
    if private_key:
        checks.extend(
            _presence_checks(
                "POLYMARKET_PRIVATE_KEY", private_key, warn_if_0x_prefixed=True
            )
        )
    else:
        checks.append(
            _missing_required_check(
                "POLYMARKET_PRIVATE_KEY",
                mode=mode,
                detail=(
                    "required for any Polymarket order (signs every request); "
                    "app.services.polymarket.client.ClobClientWrapper.initialize "
                    "raises ValueError without it."
                ),
            )
        )

    funder = settings_obj.polymarket_funder_address
    if funder:
        checks.extend(_presence_checks("POLYMARKET_FUNDER_ADDRESS", funder))
    else:
        checks.append(
            Check("pass", "POLYMARKET_FUNDER_ADDRESS: not set -- optional, falls back to None.")
        )

    api_key = settings_obj.polymarket_api_key.get_secret_value()
    api_secret = settings_obj.polymarket_api_secret.get_secret_value()
    api_passphrase = settings_obj.polymarket_api_passphrase.get_secret_value()
    checks.extend(_presence_checks("POLYMARKET_API_KEY", api_key))
    checks.extend(_presence_checks("POLYMARKET_API_SECRET", api_secret))
    checks.extend(_presence_checks("POLYMARKET_API_PASSPHRASE", api_passphrase))
    trio_set = sum(bool(v) for v in (api_key, api_secret, api_passphrase))
    if trio_set == 3:
        checks.append(
            Check("pass", "POLYMARKET_API_KEY/SECRET/PASSPHRASE: all three set; used as-is.")
        )
    elif trio_set == 0:
        checks.append(
            Check(
                "pass",
                "POLYMARKET_API_KEY/SECRET/PASSPHRASE: none set; will be derived "
                "automatically from POLYMARKET_PRIVATE_KEY at first use "
                "(create_or_derive_api_creds).",
            )
        )
    else:
        checks.append(
            Check(
                "warn",
                f"POLYMARKET_API_KEY/SECRET/PASSPHRASE: only {trio_set} of 3 set -- "
                "ClobClientWrapper.initialize requires ALL three or NONE, so this "
                "partial set is silently discarded and credentials are derived "
                "from POLYMARKET_PRIVATE_KEY instead (a setting that is set but "
                "currently inert).",
            )
        )

    key_id = settings_obj.kalshi_api_key_id
    pem = settings_obj.kalshi_private_key_pem.get_secret_value()
    checks.extend(_presence_checks("KALSHI_API_KEY_ID", key_id))
    checks.extend(_presence_checks("KALSHI_PRIVATE_KEY_PEM", pem, pem_expected=True))
    both_set = bool(key_id) and bool(pem.strip())
    if both_set:
        checks.append(Check("pass", "Kalshi credentials fully configured (key id + PEM)."))
    elif not key_id and not pem:
        checks.append(
            _missing_required_check(
                "Kalshi credentials",
                mode=mode,
                detail=(
                    "KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PEM are both required "
                    "together for any authenticated Kalshi call, order placement "
                    "included."
                ),
            )
        )
    else:
        checks.append(
            Check(
                "warn" if mode == "paper" else "fail",
                "Kalshi credentials: only one of KALSHI_API_KEY_ID/"
                "KALSHI_PRIVATE_KEY_PEM is set -- app.venues.kalshi.adapter."
                "KalshiAdapter._signing_key requires BOTH together; the lone "
                "value has no effect on its own"
                + ("." if mode == "paper" else " and live Kalshi calls will fail."),
            )
        )

    return CheckGroup("Credentials (presence and shape only -- never a value)", checks)


def _database_group(db: DatabaseCheck, expected_head: str | None) -> CheckGroup:
    """Reachability plus migration-state drift (never runs a migration)."""
    if not db.reachable:
        return CheckGroup(
            "Database",
            [
                Check(
                    "fail",
                    f"cannot connect to {db.display_url}: {db.error}. Nothing else "
                    "about schema state could be checked.",
                )
            ],
        )
    checks: list[Check] = [Check("pass", f"connected to {db.display_url}.")]
    if db.schema_error is not None:
        checks.append(
            Check(
                "fail",
                f"connected, but the schema could not be read ({db.schema_error}) "
                "-- this usually means the database has never been migrated. Run "
                "`alembic upgrade head` from an operator shell after reviewing "
                "the migration; this tool never runs it for you.",
            )
        )
    elif expected_head is None:
        checks.append(
            Check(
                "warn",
                "connected, and alembic_version reads "
                f"{db.current_revision!r}, but this process could not determine "
                "the expected head from the local migration files, so drift "
                "could not be checked.",
            )
        )
    elif db.current_revision != expected_head:
        checks.append(
            Check(
                "fail",
                f"database schema is at revision {db.current_revision!r}; code "
                f"expects head {expected_head!r} -- the schema is behind. Run "
                "`alembic upgrade head` from an operator shell after reviewing "
                "the migration; this tool never runs it for you.",
            )
        )
    else:
        checks.append(Check("pass", f"schema at head ({expected_head!r})."))
    return CheckGroup("Database", checks)


def _broker_group(broker_checks: Sequence[BrokerCheck]) -> CheckGroup:
    """Redis/Celery broker reachability -- three beats depend on this."""
    checks: list[Check] = []
    for bc in broker_checks:
        if bc.reachable:
            checks.append(Check("pass", f"{bc.label} ({bc.display_url}): reachable."))
        else:
            checks.append(
                Check(
                    "fail",
                    f"{bc.label} ({bc.display_url}): unreachable -- {bc.error}. "
                    "The reconcile, scan, and link-proposal Celery beats will "
                    "not run.",
                )
            )
    return CheckGroup("Redis / Celery broker", checks)


def _inert_settings_group(settings_obj: Settings, env: Mapping[str, str]) -> CheckGroup:
    """Configuration this repo has been bitten by before: it looks set,
    and does nothing.

    Three checks, each traced to real code rather than guessed:

    1. `REDIS_URL` is a real `Settings` field with a real default, and
       nothing in `app/` reads it -- only `CELERY_BROKER_URL`/
       `CELERY_RESULT_BACKEND` configure the Redis connection this
       system actually uses (`grep -rn "redis_url\\b" app/` finds
       exactly one hit: the field declaration itself). Flagged only
       when an operator has actually set it -- there is nothing to warn
       about in a value nobody typed.
    2. `KALSHI_BASE_URL` set to the exact production default while
       `KALSHI_ENV=demo` (the default): `Settings.kalshi_api_base_url`
       treats a `kalshi_base_url` that equals the compiled-in
       production default as "not overridden" and returns the demo URL
       regardless -- so typing the exact string shown (commented) in
       `.env.example` accomplishes nothing.
    3. A near-miss env var name: `TRADING_KILL_SWITCH_PATH` (not the
       real `KILL_SWITCH_PATH`) is the exact gotcha README.md's "Money
       safety" section already calls out by hand -- `extra="ignore"`
       drops an unmatched name with no error. This generalizes that one
       documented case to every real alias via `difflib`.
    """
    checks: list[Check] = []

    if "REDIS_URL" in env:
        checks.append(
            Check(
                "warn",
                "REDIS_URL is set, but no code in app/ reads it -- only "
                "CELERY_BROKER_URL and CELERY_RESULT_BACKEND configure the "
                "Redis connection this system actually uses. Setting REDIS_URL "
                "alone has no effect.",
            )
        )

    default_kalshi_base = type(settings_obj).model_fields["kalshi_base_url"].default
    if env.get("KALSHI_BASE_URL") == default_kalshi_base and settings_obj.kalshi_env == "demo":
        checks.append(
            Check(
                "warn",
                "KALSHI_BASE_URL is explicitly set, but to the exact production "
                "default, while KALSHI_ENV=demo (the default) -- "
                "Settings.kalshi_api_base_url treats a value equal to the "
                "production default as 'not overridden' and still returns the "
                "demo URL. Requests go to demo, not the host you set.",
            )
        )

    known_aliases = {
        f.alias or name.upper() for name, f in type(settings_obj).model_fields.items()
    }
    for key in sorted(env):
        if key in known_aliases or key in _NON_SETTINGS_ENV_NAMES:
            continue
        match = difflib.get_close_matches(key, known_aliases, n=1, cutoff=_NEAR_MISS_CUTOFF)
        if match:
            checks.append(
                Check(
                    "warn",
                    f"{key!r} is set but is not a real Settings name (did you mean "
                    f"{match[0]!r}?) -- Settings uses extra='ignore', so an "
                    "unmatched name is silently discarded rather than raising.",
                )
            )

    if not checks:
        checks.append(Check("pass", "No known inert-configuration pattern detected."))
    return CheckGroup("Settings that are set but inert", checks)


#: What this tool NEVER checks, printed on every run regardless of
#: status (GUARDRAILS.md §1.4: no venue network, ever) -- so a green
#: report cannot be mistaken for proof the venues work.
NOT_CHECKED: tuple[str, ...] = (
    "Venue connectivity -- Polymarket, Kalshi, and any Polygon RPC are never "
    "contacted by this tool (GUARDRAILS.md sec 1.4). A clean run here says "
    "nothing about whether a configured credential actually authenticates.",
    "Credential VALIDITY -- only presence and coarse shape are checked "
    "(length, a PEM prefix, a 0x prefix), never whether a venue accepts it.",
    "Whether the migration FILES apply cleanly to this database -- only the "
    "recorded alembic_version revision is compared against the local head.",
    "Whether a Celery worker or beat process is actually running and "
    "consuming from the broker -- only broker reachability is checked.",
    "Wallet or account balances at either venue.",
    "Frontend build/typecheck/lint (see README.md Development section).",
)


@dataclass(frozen=True)
class VenueCheck:
    """One venue's PUBLIC reachability probe.

    Reachability only. No credential is sent and no order endpoint is
    touched, so a clean result says the venue answered, never that your
    key works -- `NOT_CHECKED` keeps saying so even when this runs.

    Attributes:
        label: Venue name for the report line.
        url: The endpoint probed, for a legible failure.
        reachable: Whether the probe got a usable answer.
        detail: Extra fact worth printing (e.g. exchange open/closed).
        error: Failure text when `reachable` is False.
    """

    label: str
    url: str
    reachable: bool
    detail: str | None = None
    error: str | None = None


def _venue_group(venue_checks: Sequence[VenueCheck]) -> CheckGroup:
    """Render the opt-in venue reachability probes."""
    checks: list[Check] = []
    for vc in venue_checks:
        if vc.reachable:
            suffix = f" -- {vc.detail}" if vc.detail else ""
            checks.append(Check("pass", f"{vc.label}: reachable at {vc.url}{suffix}"))
        else:
            checks.append(
                Check(
                    "fail",
                    f"{vc.label}: UNREACHABLE at {vc.url} -- {vc.error}. Market data "
                    "cannot be read from this venue, so it will contribute nothing "
                    "to a scan.",
                )
            )
    return CheckGroup("Venue reachability (public endpoints, no credentials sent)", checks)


def build_report(
    settings_obj: Settings,
    *,
    db_check: DatabaseCheck,
    broker_checks: Sequence[BrokerCheck],
    expected_head_revision: str | None,
    env: Mapping[str, str],
    venue_checks: Sequence[VenueCheck] | None = None,
) -> PreflightReport:
    """Assemble the full report from already-resolved inputs.

    A pure function: every network-shaped fact (`db_check`,
    `broker_checks`) is passed in pre-computed, and `env` is an explicit
    mapping rather than a read of `os.environ` -- so a test can build
    an arbitrary scenario (an unreachable database, a schema behind
    head, a near-miss env var name) without touching a real database,
    Redis, or process environment, and without needing `TRADING_MODE`
    to be anything but `settings_obj.trading_mode` (GUARDRAILS.md sec
    1.2: `TRADING_MODE` in the actual test PROCESS always stays
    `"paper"`; what this function checks is the explicit `Settings`
    object it was handed).

    Args:
        settings_obj: The `Settings` instance to check.
        db_check: Pre-computed database connectivity/schema result.
        broker_checks: Pre-computed broker reachability result(s).
        expected_head_revision: The local migration files' head
            revision, or `None` if it could not be determined.
        env: The environment mapping to scan for inert-setting
            patterns -- the real CLI passes `os.environ`.

    Returns:
        PreflightReport: Every group, plus the fixed `NOT_CHECKED` list.
    """
    groups = [
        _trading_mode_group(settings_obj),
        _credentials_group(settings_obj),
        _database_group(db_check, expected_head_revision),
        _broker_group(broker_checks),
        _inert_settings_group(settings_obj, env),
    ]
    not_checked = list(NOT_CHECKED)
    if venue_checks is not None:
        groups.append(_venue_group(venue_checks))
        # Reachability WAS checked, so that line would now be false. The
        # credential-validity line stays: a public probe sends no key.
        not_checked = [
            item for item in not_checked if not item.startswith("Venue connectivity")
        ]
        not_checked.insert(
            0,
            "Venue AUTHENTICATION -- the probe above is a public endpoint and sends "
            "no credential, so a reachable venue still says nothing about whether "
            "your API key is accepted.",
        )
    return PreflightReport(groups=groups, not_checked=not_checked)


# ---------------------------------------------------------------------------
# Real (network-touching) default checkers -- used only by `main()`, never
# imported by tests. `backend/tests/test_preflight.py` builds `DatabaseCheck`/
# `BrokerCheck` values by hand and feeds them straight to `build_report`.
# ---------------------------------------------------------------------------


async def _check_database_async(settings_obj: Settings, timeout_s: float) -> DatabaseCheck:
    """Open one short-lived connection, read `alembic_version`, close it.

    Never issues DDL and never calls anything from `alembic.command`
    (GUARDRAILS.md: "never run `alembic upgrade head`") -- this is a
    plain `SELECT` against a table alembic itself maintains.
    """
    display_url = _redact_url(settings_obj.database_url)
    engine = create_async_engine(settings_obj.async_database_url, poolclass=NullPool)
    try:
        try:
            conn = await asyncio.wait_for(engine.connect(), timeout=timeout_s)
        except Exception as exc:
            return DatabaseCheck(
                reachable=False, display_url=display_url, error=f"{type(exc).__name__}: {exc}"
            )
        try:
            try:
                result = await asyncio.wait_for(
                    conn.execute(text("SELECT version_num FROM alembic_version")),
                    timeout=timeout_s,
                )
                row = result.first()
                revision = str(row[0]) if row is not None else None
                return DatabaseCheck(
                    reachable=True, display_url=display_url, current_revision=revision
                )
            except Exception as exc:
                return DatabaseCheck(
                    reachable=True,
                    display_url=display_url,
                    schema_error=f"{type(exc).__name__}: {exc}",
                )
        finally:
            await conn.close()
    finally:
        await engine.dispose()


def default_check_database(settings_obj: Settings, timeout_s: float = 3.0) -> DatabaseCheck:
    """Real database check: connects and reads schema state. Not used by tests."""
    try:
        return asyncio.run(_check_database_async(settings_obj, timeout_s))
    except Exception as exc:  # defensive: bad URL scheme, event-loop setup, etc.
        return DatabaseCheck(
            reachable=False,
            display_url=_redact_url(settings_obj.database_url),
            error=f"{type(exc).__name__}: {exc}",
        )


def default_check_broker(label: str, url: str, timeout_s: float = 2.0) -> BrokerCheck:
    """Real broker check: opens a connection and PINGs it. Not used by tests."""
    display_url = _redact_url(url)
    client = redis.Redis.from_url(url, socket_connect_timeout=timeout_s, socket_timeout=timeout_s)
    try:
        client.ping()
        return BrokerCheck(label=label, display_url=display_url, reachable=True)
    except Exception as exc:
        return BrokerCheck(
            label=label,
            display_url=display_url,
            reachable=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        client.close()


def _expected_head_revision() -> str:
    """Read the local migration files' head revision. Touches no database.

    Uses `alembic.script.ScriptDirectory`, which only walks `.py` files
    under `alembic/versions/` -- it does not execute `alembic/env.py`
    (which is what would need a database URL) and it does not import
    or run any migration. GUARDRAILS.md sec 2: "alembic heads is 007";
    this reads that fact from the same files that make it true, rather
    than hardcoding "007" and risking drift when 008 lands.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    backend_dir = Path(__file__).resolve().parent.parent.parent
    cfg = Config(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"expected exactly one alembic head, found {heads!r}")
    return heads[0]



def default_check_venues(settings_obj: Settings, timeout_s: float = 8.0) -> list[VenueCheck]:
    """Probe both venues' PUBLIC endpoints. Not used by tests.

    Opt-in only (`--check-venues`), because the default report promises it
    contacts no venue and that promise is worth keeping literally.

    Both probes are unauthenticated GETs against read-only status/listing
    endpoints. No credential is sent, nothing is written, and no order
    endpoint is touched — so this answers "can I reach the venue", never
    "is my key accepted".

    Kalshi's `/exchange/status` also reports whether the exchange is OPEN,
    which is worth surfacing: a reachable venue with a closed exchange
    produces empty books, and an empty book is indistinguishable from a
    quiet market unless something says so.
    """
    import httpx

    checks: list[VenueCheck] = []

    kalshi_url = f"{settings_obj.kalshi_api_base_url}/exchange/status"
    try:
        response = httpx.get(kalshi_url, timeout=timeout_s)
        response.raise_for_status()
        body = response.json()
        active = body.get("exchange_active")
        trading = body.get("trading_active")
        checks.append(
            VenueCheck(
                label=f"Kalshi ({settings_obj.kalshi_env})",
                url=kalshi_url,
                reachable=True,
                detail=f"exchange_active={active}, trading_active={trading}",
            )
        )
    except Exception as exc:
        checks.append(
            VenueCheck(
                label=f"Kalshi ({settings_obj.kalshi_env})",
                url=kalshi_url,
                reachable=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        )

    gamma_url = f"{settings_obj.gamma_api_url}/markets"
    try:
        response = httpx.get(gamma_url, params={"limit": 1}, timeout=timeout_s)
        response.raise_for_status()
        payload = response.json()
        items = payload.get("data", payload) if isinstance(payload, dict) else payload
        checks.append(
            VenueCheck(
                label="Polymarket (Gamma)",
                url=gamma_url,
                reachable=True,
                detail=f"listing returned {len(items)} market(s)",
            )
        )
    except Exception as exc:
        checks.append(
            VenueCheck(
                label="Polymarket (Gamma)",
                url=gamma_url,
                reachable=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        )
    return checks


def main() -> int:
    """Real CLI entry point: checks the process-wide `Settings`.

    `--check-venues` additionally probes both venues' PUBLIC endpoints.
    It is opt-in so the default run keeps its promise of contacting no
    venue at all, and even with it the report still says authentication
    was not checked — the probe sends no credential.

    Returns:
        int: The process exit code (`PreflightReport.exit_code`).
    """
    check_venues = "--check-venues" in sys.argv[1:]
    try:
        settings_obj = get_settings()
    except Exception as exc:
        print(
            f"FAIL: could not construct Settings at all: {type(exc).__name__}: {exc}\n"
            "Fix the invalid environment/.env value this names before rerunning -- "
            "no further checks can run without a loadable Settings instance.",
            file=sys.stderr,
        )
        return 1

    db_check = default_check_database(settings_obj)

    broker_checks: list[BrokerCheck] = [
        default_check_broker("CELERY_BROKER_URL", settings_obj.celery_broker_url)
    ]
    if settings_obj.celery_result_backend != settings_obj.celery_broker_url:
        broker_checks.append(
            default_check_broker("CELERY_RESULT_BACKEND", settings_obj.celery_result_backend)
        )

    expected_head: str | None
    try:
        expected_head = _expected_head_revision()
    except Exception:
        expected_head = None

    report = build_report(
        settings_obj,
        db_check=db_check,
        broker_checks=broker_checks,
        expected_head_revision=expected_head,
        env=os.environ,
        venue_checks=default_check_venues(settings_obj) if check_venues else None,
    )
    print(report.render())
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
