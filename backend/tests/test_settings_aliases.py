"""`Settings` must accept BOTH the field name and the env-var alias.

This guards a hazard that made a MONEY FENCE test vacuous. Every
`Settings` field carries an explicit `alias=` (the SCREAMING_CASE env var
name) and `model_config` sets `extra="ignore"`, so before
`populate_by_name=True` was added the natural, field-named keyword was
accepted without error and then silently DISCARDED:

    Settings(trading_mode="live").trading_mode  ->  'paper'   (!!)
    Settings(TRADING_MODE="live").trading_mode  ->  'live'

GUARDRAILS.md §1.2 instructs every live-trading fence test to "construct
explicit `Settings` objects and pass them as parameters". A test that did
exactly that with the field name would have exercised PAPER mode while
believing it had proved the LIVE branch -- and would have passed. These
tests exist so that regression cannot come back unnoticed.
"""
import os

import pytest

from app.config import KALSHI_DEMO_BASE_URL, Settings


@pytest.mark.parametrize("keyword", ["trading_mode", "TRADING_MODE"])
def test_trading_mode_accepts_field_name_and_alias(keyword: str) -> None:
    """Both spellings must actually take effect -- neither is swallowed."""
    settings = Settings(**{keyword: "live"})

    assert settings.trading_mode == "live"


@pytest.mark.parametrize(
    "keyword", ["live_trading_confirmation", "LIVE_TRADING_CONFIRMATION"]
)
def test_live_trading_confirmation_accepts_field_name_and_alias(keyword: str) -> None:
    """The other half of the fence, same hazard."""
    settings = Settings(**{keyword: "I_UNDERSTAND_REAL_MONEY"})

    assert settings.live_trading_confirmation == "I_UNDERSTAND_REAL_MONEY"


def test_defaults_are_still_paper_and_unconfirmed() -> None:
    """GUARDRAILS.md §1.2: paper is the default and stays the default."""
    settings = Settings()

    assert settings.trading_mode == "paper"
    assert settings.live_trading_confirmation == ""


def test_env_var_loading_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """`populate_by_name` widens keyword input; it must not narrow env input."""
    monkeypatch.setenv("KALSHI_TAKER_FEE_RATE", "0.09")

    assert Settings().kalshi_taker_fee_rate == pytest.approx(0.09)


def test_kalshi_env_defaults_to_demo() -> None:
    """Production Kalshi is opt-in, like live trading."""
    settings = Settings()

    assert settings.kalshi_env == "demo"
    assert settings.kalshi_api_base_url == KALSHI_DEMO_BASE_URL


def test_kalshi_prod_env_selects_the_production_base() -> None:
    settings = Settings(kalshi_env="prod")

    assert settings.kalshi_api_base_url == "https://external-api.kalshi.com/trade-api/v2"


def test_an_explicit_base_url_overrides_the_env_selector() -> None:
    """A deliberate override is never silently replaced by the demo URL."""
    alternate = "https://api.elections.kalshi.com/trade-api/v2"
    settings = Settings(kalshi_env="demo", kalshi_base_url=alternate)

    assert settings.kalshi_api_base_url == alternate


def test_the_kalshi_private_key_is_a_secret() -> None:
    """GUARDRAILS.md §1.3: the PEM must not appear in a repr of Settings."""
    settings = Settings(kalshi_private_key_pem="pretend-pem-material")

    assert "pretend-pem-material" not in repr(settings)
    assert settings.kalshi_private_key_pem.get_secret_value() == "pretend-pem-material"


def test_the_test_process_reads_no_env_file() -> None:
    """Tests must describe the code, not the machine they run on.

    `Settings` anchors `.env` to the REPO ROOT so the documented
    workflow (`cd backend && ...`) actually loads configuration. The
    side effect is that a developer's real `.env` would otherwise load
    into every test process: it broke three tests asserting defaults the
    moment a real one appeared, and it puts live credentials one
    careless `print` away from a test log (GUARDRAILS.md §1.3).

    `tests/conftest.py` sets `POLYMARKET_TRADER_ENV_FILE=""` before any
    `app.*` import. This asserts that guard is still in place, so
    deleting it fails here with the reason rather than showing up later
    as tests that pass or fail depending on whose laptop they run on.
    """
    assert os.environ.get("POLYMARKET_TRADER_ENV_FILE") == "", (
        "conftest.py must disable .env loading for tests; without it this "
        "process inherits the developer's real credentials"
    )
    assert Settings().kalshi_api_key_id == ""
