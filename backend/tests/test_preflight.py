"""Tests for `app.scripts.preflight` (T45).

GUARDRAILS.md §1.2: `TRADING_MODE` stays `"paper"` in this test PROCESS
always -- every scenario below (including the "live mode" ones) is
built by constructing an explicit `Settings(...)` object and passing it
straight to `build_report`, never by mutating `os.environ` or the
process-wide `app.config.settings` singleton. `build_report` never
touches a database, Redis, or a venue: `DatabaseCheck`/`BrokerCheck`
results are constructed by hand here, exactly as `app.scripts.preflight`
module docstring says a caller should for tests.
"""
from pathlib import Path

import pytest

from app.config import Settings
from app.execution.fences import LIVE_TRADING_CONFIRMATION_PHRASE
from app.scripts.preflight import (
    BrokerCheck,
    Check,
    CheckGroup,
    DatabaseCheck,
    PreflightReport,
    Status,
    VenueCheck,
    _redact_url,
    build_report,
)

#: A database/broker state that never fails or warns on its own, so a
#: test can focus its assertions on one other group without the
#: database/broker checks adding noise (or, worse, a spurious FAIL) of
#: their own.
_OK_DB = DatabaseCheck(
    reachable=True, display_url="postgresql+asyncpg://x:***@localhost/db", current_revision="007"
)
_OK_BROKER = [BrokerCheck(label="CELERY_BROKER_URL", display_url="redis://localhost:6379/0", reachable=True)]


def _report(
    settings_obj: Settings,
    *,
    db_check: DatabaseCheck = _OK_DB,
    broker_checks: list[BrokerCheck] = _OK_BROKER,
    expected_head_revision: str | None = "007",
    env: dict[str, str] | None = None,
) -> PreflightReport:
    return build_report(
        settings_obj,
        db_check=db_check,
        broker_checks=broker_checks,
        expected_head_revision=expected_head_revision,
        env={} if env is None else env,
    )


# ---------------------------------------------------------------------------
# 1. A clean paper-mode run.
# ---------------------------------------------------------------------------


def test_clean_paper_mode_run_exits_zero(tmp_path: Path) -> None:
    """Default `Settings()` (paper, no credentials, no kill switch), with a
    healthy database and broker: warnings about missing (optional-in-paper)
    credentials are expected and fine, but the exit code must be 0."""
    cfg = Settings(KILL_SWITCH_PATH=str(tmp_path / "TRADING_KILL_SWITCH"))

    report = _report(cfg)

    assert report.status != "fail", report.render()
    assert report.exit_code == 0
    assert "DECISION: this process would NOT place real orders right now." in report.render()


# ---------------------------------------------------------------------------
# 2. Live mode, LIVE_TRADING_CONFIRMATION unset.
# ---------------------------------------------------------------------------


def test_live_mode_without_confirmation_fails_and_names_it(tmp_path: Path) -> None:
    cfg = Settings(
        TRADING_MODE="live",
        LIVE_TRADING_CONFIRMATION="",
        KILL_SWITCH_PATH=str(tmp_path / "TRADING_KILL_SWITCH"),
    )

    report = _report(cfg)

    assert report.status == "fail"
    assert report.exit_code == 1
    fail_messages = [
        c.message for g in report.groups for c in g.checks if c.status == "fail"
    ]
    assert any("LIVE_TRADING_CONFIRMATION" in m for m in fail_messages), report.render()


# ---------------------------------------------------------------------------
# 3. Kill switch: warn in paper, warn-but-decisive in live.
# ---------------------------------------------------------------------------


def test_kill_switch_present_warns_in_paper_mode(tmp_path: Path) -> None:
    switch = tmp_path / "TRADING_KILL_SWITCH"
    switch.write_text("", encoding="utf-8")
    cfg = Settings(TRADING_MODE="paper", KILL_SWITCH_PATH=str(switch))

    report = _report(cfg)

    warn_messages = [
        c.message for g in report.groups for c in g.checks if c.status == "warn"
    ]
    assert any("Kill switch file present" in m and "no effect" in m for m in warn_messages)
    assert report.status != "fail", report.render()


def test_kill_switch_present_is_decisive_but_still_a_warn_in_live_mode(tmp_path: Path) -> None:
    """Live mode, fully armed (confirmed, all credentials present), but a
    kill-switch file exists: per "The bar" in the brief, this is a WARN
    ("this works but you probably did not mean it"), not a FAIL -- the
    system is correctly, deliberately halted."""
    switch = tmp_path / "TRADING_KILL_SWITCH"
    switch.write_text("", encoding="utf-8")
    cfg = Settings(
        TRADING_MODE="live",
        LIVE_TRADING_CONFIRMATION=LIVE_TRADING_CONFIRMATION_PHRASE,
        KILL_SWITCH_PATH=str(switch),
        POLYMARKET_PRIVATE_KEY="a" * 64,
        POLYMARKET_API_KEY="k",
        POLYMARKET_API_SECRET="s",
        POLYMARKET_API_PASSPHRASE="p",
        KALSHI_API_KEY_ID="key-id",
        KALSHI_PRIVATE_KEY_PEM=(
            "-----BEGIN RSA PRIVATE KEY-----\nfakefakefake\n-----END RSA PRIVATE KEY-----\n"
        ),
    )

    report = _report(cfg)

    assert report.status == "warn", report.render()
    assert report.exit_code == 0
    warn_messages = [
        c.message for g in report.groups for c in g.checks if c.status == "warn"
    ]
    assert any(
        "Kill switch file present" in m and "ONLY" in m and "preventing live order placement" in m
        for m in warn_messages
    ), report.render()


# ---------------------------------------------------------------------------
# 4. Missing credential: fail in live, warn in paper.
# ---------------------------------------------------------------------------


def test_missing_kalshi_credential_fails_in_live_mode(tmp_path: Path) -> None:
    cfg = Settings(
        TRADING_MODE="live",
        LIVE_TRADING_CONFIRMATION=LIVE_TRADING_CONFIRMATION_PHRASE,
        KILL_SWITCH_PATH=str(tmp_path / "TRADING_KILL_SWITCH"),
        # Polymarket left fully unconfigured too -- only asserting on Kalshi
        # below, but the report should show BOTH as failing.
    )

    report = _report(cfg)

    assert report.status == "fail"
    assert report.exit_code == 1
    fail_messages = [
        c.message for g in report.groups for c in g.checks if c.status == "fail"
    ]
    assert any("KALSHI_API_KEY_ID" in m and "KALSHI_PRIVATE_KEY_PEM" in m for m in fail_messages), (
        report.render()
    )
    assert any("POLYMARKET_PRIVATE_KEY" in m for m in fail_messages), report.render()


def test_missing_kalshi_credential_warns_in_paper_mode(tmp_path: Path) -> None:
    cfg = Settings(TRADING_MODE="paper", KILL_SWITCH_PATH=str(tmp_path / "TRADING_KILL_SWITCH"))

    report = _report(cfg)

    assert report.status != "fail", report.render()
    warn_messages = [
        c.message for g in report.groups for c in g.checks if c.status == "warn"
    ]
    assert any(
        "KALSHI_API_KEY_ID" in m and "KALSHI_PRIVATE_KEY_PEM" in m and "fine for paper mode" in m
        for m in warn_messages
    ), report.render()


def test_partial_polymarket_api_trio_is_flagged_as_inert_in_either_mode(tmp_path: Path) -> None:
    """Only 1 of 3 API-credential fields set: silently discarded in favor
    of derivation from the private key -- a real "set but inert" case,
    not a plain missing-credential one, regardless of trading mode."""
    cfg = Settings(
        TRADING_MODE="paper",
        KILL_SWITCH_PATH=str(tmp_path / "TRADING_KILL_SWITCH"),
        POLYMARKET_PRIVATE_KEY="a" * 64,
        POLYMARKET_API_KEY="only-this-one-set",
    )

    report = _report(cfg)

    warn_messages = [
        c.message for g in report.groups for c in g.checks if c.status == "warn"
    ]
    assert any(
        "only 1 of 3 set" in m and "silently discarded" in m for m in warn_messages
    ), report.render()


# ---------------------------------------------------------------------------
# 5. Unreachable database.
# ---------------------------------------------------------------------------


def test_unreachable_database_fails(tmp_path: Path) -> None:
    cfg = Settings(KILL_SWITCH_PATH=str(tmp_path / "TRADING_KILL_SWITCH"))
    db_check = DatabaseCheck(
        reachable=False,
        display_url="postgresql+asyncpg://x:***@localhost/db",
        error="ConnectionRefusedError: [Errno 61] Connect call failed",
    )

    report = _report(cfg, db_check=db_check)

    assert report.status == "fail"
    assert report.exit_code == 1
    fail_messages = [
        c.message for g in report.groups for c in g.checks if c.status == "fail"
    ]
    assert any("cannot connect" in m and "ConnectionRefusedError" in m for m in fail_messages)


# ---------------------------------------------------------------------------
# 6. Schema behind head.
# ---------------------------------------------------------------------------


def test_schema_behind_head_fails_naming_the_revision_gap(tmp_path: Path) -> None:
    cfg = Settings(KILL_SWITCH_PATH=str(tmp_path / "TRADING_KILL_SWITCH"))
    db_check = DatabaseCheck(
        reachable=True,
        display_url="postgresql+asyncpg://x:***@localhost/db",
        current_revision="005",
    )

    report = _report(cfg, db_check=db_check, expected_head_revision="007")

    assert report.status == "fail"
    fail_messages = [
        c.message for g in report.groups for c in g.checks if c.status == "fail"
    ]
    assert any("'005'" in m and "'007'" in m and "behind" in m for m in fail_messages), (
        report.render()
    )


def test_unreachable_broker_fails(tmp_path: Path) -> None:
    cfg = Settings(KILL_SWITCH_PATH=str(tmp_path / "TRADING_KILL_SWITCH"))
    broker_checks = [
        BrokerCheck(
            label="CELERY_BROKER_URL",
            display_url="redis://localhost:6379/0",
            reachable=False,
            error="ConnectionError: Error 61 connecting to localhost:6379",
        )
    ]

    report = _report(cfg, broker_checks=broker_checks)

    assert report.status == "fail"
    fail_messages = [
        c.message for g in report.groups for c in g.checks if c.status == "fail"
    ]
    assert any("unreachable" in m and "beats will not run" in m for m in fail_messages)


# ---------------------------------------------------------------------------
# 7. THE §1.3 GUARD: no secret value ever appears in the output.
# ---------------------------------------------------------------------------


def test_no_secret_value_ever_appears_in_the_rendered_report(tmp_path: Path) -> None:
    """Feed every credential field a recognisable, obviously-fake value and
    assert none of those exact strings appear anywhere in the rendered
    report -- this is the §1.3 guard and matters more than the rest."""
    fake_private_key = "FAKEPOLYPRIVATEKEYSHOULDNEVERAPPEAR0123456789abcdef"
    fake_funder = "0xFAKEFUNDERADDRESSSHOULDNEVERAPPEAR"
    fake_api_key = "FAKEAPIKEYSHOULDNEVERAPPEAR"
    fake_api_secret = "FAKEAPISECRETSHOULDNEVERAPPEAR"
    fake_api_passphrase = "FAKEAPIPASSPHRASESHOULDNEVERAPPEAR"
    fake_kalshi_key_id = "FAKEKALSHIKEYIDSHOULDNEVERAPPEAR"
    fake_pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "FAKEPEMBODYSHOULDNEVERAPPEARINANYOUTPUT1234567890\n"
        "-----END RSA PRIVATE KEY-----\n"
    )
    cfg = Settings(
        TRADING_MODE="live",
        LIVE_TRADING_CONFIRMATION=LIVE_TRADING_CONFIRMATION_PHRASE,
        KILL_SWITCH_PATH=str(tmp_path / "TRADING_KILL_SWITCH"),
        POLYMARKET_PRIVATE_KEY=fake_private_key,
        POLYMARKET_FUNDER_ADDRESS=fake_funder,
        POLYMARKET_API_KEY=fake_api_key,
        POLYMARKET_API_SECRET=fake_api_secret,
        POLYMARKET_API_PASSPHRASE=fake_api_passphrase,
        KALSHI_API_KEY_ID=fake_kalshi_key_id,
        KALSHI_PRIVATE_KEY_PEM=fake_pem,
        DATABASE_URL="postgresql+asyncpg://produser:SUPERSECRETPASSWORD@localhost:5432/db",
    )
    db_check = DatabaseCheck(
        reachable=False,
        display_url="postgresql+asyncpg://produser:***@localhost:5432/db",
        error="timeout",
    )

    report = _report(cfg, db_check=db_check)
    rendered = report.render()

    for secret in (
        fake_private_key,
        fake_funder,
        fake_api_key,
        fake_api_secret,
        fake_api_passphrase,
        fake_kalshi_key_id,
        fake_pem,
        "FAKEPEMBODYSHOULDNEVERAPPEARINANYOUTPUT1234567890",
        "SUPERSECRETPASSWORD",
    ):
        assert secret not in rendered, f"secret value leaked into report: {secret!r}"


# ---------------------------------------------------------------------------
# Settings that are set but inert.
# ---------------------------------------------------------------------------


def test_redis_url_flagged_as_inert_when_set(tmp_path: Path) -> None:
    cfg = Settings(KILL_SWITCH_PATH=str(tmp_path / "TRADING_KILL_SWITCH"))

    report = _report(cfg, env={"REDIS_URL": "redis://somewhere:6379"})

    warn_messages = [
        c.message for g in report.groups for c in g.checks if c.status == "warn"
    ]
    assert any(
        "REDIS_URL is set" in m and "CELERY_BROKER_URL" in m for m in warn_messages
    ), report.render()


def test_redis_url_not_flagged_when_unset(tmp_path: Path) -> None:
    cfg = Settings(KILL_SWITCH_PATH=str(tmp_path / "TRADING_KILL_SWITCH"))

    report = _report(cfg, env={})

    warn_messages = [
        c.message for g in report.groups for c in g.checks if c.status == "warn"
    ]
    assert not any("REDIS_URL is set" in m for m in warn_messages)


def test_kalshi_base_url_trap_is_flagged(tmp_path: Path) -> None:
    """Explicitly setting KALSHI_BASE_URL to the exact production default
    while KALSHI_ENV=demo is silently ignored by
    Settings.kalshi_api_base_url -- see that property's docstring."""
    default_prod = Settings.model_fields["kalshi_base_url"].default
    cfg = Settings(KILL_SWITCH_PATH=str(tmp_path / "TRADING_KILL_SWITCH"), KALSHI_ENV="demo")
    assert cfg.kalshi_api_base_url != default_prod  # sanity: demo wins here

    report = _report(cfg, env={"KALSHI_BASE_URL": default_prod})

    warn_messages = [
        c.message for g in report.groups for c in g.checks if c.status == "warn"
    ]
    assert any("KALSHI_BASE_URL" in m and "demo" in m for m in warn_messages), report.render()


def test_near_miss_env_var_name_is_flagged(tmp_path: Path) -> None:
    """The exact gotcha README.md's Money safety section documents by
    hand: TRADING_KILL_SWITCH_PATH is not a real Settings alias
    (KILL_SWITCH_PATH is) and is silently ignored (extra='ignore')."""
    cfg = Settings(KILL_SWITCH_PATH=str(tmp_path / "TRADING_KILL_SWITCH"))

    report = _report(cfg, env={"TRADING_KILL_SWITCH_PATH": str(tmp_path / "oops")})

    warn_messages = [
        c.message for g in report.groups for c in g.checks if c.status == "warn"
    ]
    assert any(
        "TRADING_KILL_SWITCH_PATH" in m and "KILL_SWITCH_PATH" in m for m in warn_messages
    ), report.render()


def test_ordinary_env_vars_are_not_flagged_as_near_misses(tmp_path: Path) -> None:
    """Common shell/OS variables must not trip the near-miss detector --
    it would drown the real signal in noise."""
    cfg = Settings(KILL_SWITCH_PATH=str(tmp_path / "TRADING_KILL_SWITCH"))

    report = _report(
        cfg,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": "/Users/x",
            "LANG": "en_US.UTF-8",
            "PYTHONPATH": "/some/path",
            "CI": "true",
        },
    )

    warn_messages = [
        c.message for g in report.groups for c in g.checks if c.status == "warn"
    ]
    assert not any("did you mean" in m for m in warn_messages), report.render()


# ---------------------------------------------------------------------------
# Rendering / structure.
# ---------------------------------------------------------------------------


def test_render_includes_a_not_checked_section_naming_venue_connectivity(tmp_path: Path) -> None:
    cfg = Settings(KILL_SWITCH_PATH=str(tmp_path / "TRADING_KILL_SWITCH"))

    rendered = _report(cfg).render()

    assert "NOT checked by this tool" in rendered
    assert "Venue connectivity" in rendered
    assert "Polymarket" in rendered and "Kalshi" in rendered


@pytest.mark.parametrize("status", ["pass", "warn", "fail"])
def test_exit_code_only_reacts_to_fail(status: Status) -> None:
    """A tool that fails the run on a warning gets ignored -- only FAIL
    may set a nonzero exit code."""
    report = PreflightReport(groups=[CheckGroup("x", [Check(status, "msg")])], not_checked=[])

    assert report.exit_code == (1 if status == "fail" else 0)


# ---------------------------------------------------------------------------
# URL password redaction (§1.3).
# ---------------------------------------------------------------------------
#
# `test_no_secret_value_ever_appears_in_the_rendered_report` asserts that
# "SUPERSECRETPASSWORD" never reaches the report, but it hands `build_report`
# a `display_url` that is ALREADY masked -- so that assertion holds no matter
# what `_redact_url` does, and cannot fail if redaction breaks. Redaction is
# the single most safety-critical line in this module and was the one thing
# with no direct coverage. These tests exercise it head-on.


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The two shapes this module actually prints.
        (
            "postgresql+asyncpg://produser:SUPERSECRETPASSWORD@localhost:5432/db",
            "postgresql+asyncpg://produser:***@localhost:5432/db",
        ),
        ("redis://:SUPERSECRETPASSWORD@localhost:6379/0", "redis://:***@localhost:6379/0"),
        ("redis://user:SUPERSECRETPASSWORD@10.0.0.4:6379/1", "redis://user:***@10.0.0.4:6379/1"),
        # No userinfo password -- must pass through untouched, because the
        # host/port/database name are not secret and are the useful half of
        # the line for an operator reading a connection failure.
        ("postgresql+asyncpg://localhost:5432/db", "postgresql+asyncpg://localhost:5432/db"),
        ("redis://localhost:6379/0", "redis://localhost:6379/0"),
        # A username but no password is not a password.
        ("postgresql+asyncpg://produser@localhost:5432/db", "postgresql+asyncpg://produser@localhost:5432/db"),
    ],
)
def test_redact_url_masks_only_the_password(raw: str, expected: str) -> None:
    assert _redact_url(raw) == expected


def test_redact_url_keeps_the_useful_half_of_the_line() -> None:
    """A redacted URL must still identify WHICH database failed.

    Masking the whole URL would satisfy §1.3 and make the report useless --
    an operator reading a connection failure needs the host, port and
    database name to act on it.
    """
    redacted = _redact_url("postgresql+asyncpg://produser:hunter2@db.internal:5432/polymarket")

    assert "hunter2" not in redacted
    for keep in ("produser", "db.internal", "5432", "polymarket"):
        assert keep in redacted


def test_redact_url_masks_a_password_containing_url_punctuation() -> None:
    """Real generated passwords carry `+`, `=`, `.` and `-`.

    The regex stops at `@`, `/` and whitespace, so a password holding any
    of those would be a real leak; these are the characters a base64-ish
    generated secret actually contains.
    """
    redacted = _redact_url("postgresql+asyncpg://u:aB3+xY9=zQ.w-1@localhost:5432/db")

    assert redacted == "postgresql+asyncpg://u:***@localhost:5432/db"
    assert "aB3+xY9=zQ.w-1" not in redacted


# ---------------------------------------------------------------------------
# Opt-in venue reachability (--check-venues).
# ---------------------------------------------------------------------------
#
# The default report promises it contacts no venue, and that promise is
# worth keeping literally -- so the probe is opt-in and the default path
# must stay unchanged. Even when it runs it sends NO credential, so
# "reachable" must never be allowed to read as "my API key works".


def test_by_default_no_venue_is_contacted_and_the_report_says_so(tmp_path: Path) -> None:
    report = _report(Settings(KILL_SWITCH_PATH=str(tmp_path / "TRADING_KILL_SWITCH")))

    assert not any("Venue reachability" in g.name for g in report.groups)
    assert any(item.startswith("Venue connectivity") for item in report.not_checked)


def test_a_reachable_venue_never_claims_authentication_was_checked(
    tmp_path: Path,
) -> None:
    cfg = Settings(KILL_SWITCH_PATH=str(tmp_path / "TRADING_KILL_SWITCH"))

    report = build_report(
        cfg,
        db_check=_OK_DB,
        broker_checks=_OK_BROKER,
        expected_head_revision="007",
        env={},
        venue_checks=[
            VenueCheck(label="Kalshi (prod)", url="https://x/exchange/status",
                       reachable=True, detail="exchange_active=True"),
        ],
    )

    assert any("Venue reachability" in g.name for g in report.groups)
    # The now-false blanket claim is gone; the narrower true one replaces it.
    assert not any(item.startswith("Venue connectivity") for item in report.not_checked)
    assert any(item.startswith("Venue AUTHENTICATION") for item in report.not_checked)
    assert report.status != "fail"


def test_an_unreachable_venue_fails_and_says_what_it_costs(tmp_path: Path) -> None:
    cfg = Settings(KILL_SWITCH_PATH=str(tmp_path / "TRADING_KILL_SWITCH"))

    report = build_report(
        cfg,
        db_check=_OK_DB,
        broker_checks=_OK_BROKER,
        expected_head_revision="007",
        env={},
        venue_checks=[
            VenueCheck(label="Kalshi (prod)", url="https://x/exchange/status",
                       reachable=False, error="ConnectError: nope"),
        ],
    )

    assert report.status == "fail"
    fails = [c.message for g in report.groups for c in g.checks if c.status == "fail"]
    assert any("UNREACHABLE" in m and "contribute nothing to a scan" in m for m in fails)
