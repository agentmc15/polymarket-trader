"""Application configuration using Pydantic Settings."""
from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from pydantic_settings.sources import InitSettingsSource


class _AliasNormalizedInitSource(InitSettingsSource):
    """`InitSettingsSource` that re-keys constructor kwargs to field ALIASES.

    THIS EXISTS TO KEEP A MONEY-FENCE TEST FROM GOING VACUOUS, and
    `model_config`'s `populate_by_name=True` is only half of that fix.

    The other half is a precedence bug that `populate_by_name` alone does
    NOT solve. Settings values are merged from several sources before
    validation, and `EnvSettingsSource` keys its values by a field's
    ALIAS (`{"TRADING_MODE": "paper"}`) while `InitSettingsSource` keys
    them by whatever the caller typed (`{"trading_mode": "live"}`). Both
    keys therefore survive into the same input dict, and pydantic
    resolves that collision in favour of the ALIAS — so the environment
    beat the explicit constructor argument:

        # with TRADING_MODE=paper in the environment, as
        # `tests/conftest.py` sets for the whole test session:
        Settings(trading_mode="live").trading_mode  ->  'paper'   (!!)

    That is precisely the shape GUARDRAILS.md §1.2 tells fence tests to
    use ("construct explicit `Settings` objects and pass them as
    parameters"), and it was silently exercising PAPER mode while
    claiming to prove the LIVE branch — a test that passes while testing
    nothing, inside the fence that guards real money.

    Re-keying init kwargs to their alias makes the constructor and the
    environment collide on ONE key, which the documented source
    precedence (init first, i.e. highest) then resolves the right way
    round. Aliased or not, `Settings(TRADING_MODE=...)` and
    `Settings(trading_mode=...)` now mean the same thing and both beat
    the environment.
    """

    def __call__(self) -> dict[str, Any]:
        """Return the init kwargs with every field name replaced by its alias."""
        alias_by_field_name = {
            name: field.alias
            for name, field in self.settings_cls.model_fields.items()
            if field.alias
        }
        return {
            alias_by_field_name.get(key, key): value
            for key, value in super().__call__().items()
        }


#: Kalshi's demo (paper) Trade API v2 base URL (PLAN.md §3 Venue facts,
#: docs.kalshi.com, pinned 2026-09-04). Selected by `kalshi_env="demo"`,
#: which is the default — see `Settings.kalshi_api_base_url`.
KALSHI_DEMO_BASE_URL = "https://external-api.demo.kalshi.co/trade-api/v2"

#: Venue keys `Settings.paper_starting_balances` accepts. Mirrors
#: `app.venues.types.VenueId` by hand — see that field's comment for why
#: `app.config` does not import from `app.venues`.
_PAPER_LEDGER_VENUES = frozenset({"polymarket", "kalshi"})


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    A CONSTRUCTOR ARGUMENT MUST TAKE EFFECT, WHICHEVER SPELLING IS USED.
    Almost every field below carries an explicit `alias=` (the
    SCREAMING_CASE env var name), and `extra="ignore"` silently discards
    any keyword matching neither an alias nor a field name. That
    combination produced two distinct, silent failures, and BOTH are
    fixed here — `populate_by_name=True` below, plus
    `_AliasNormalizedInitSource` (see `settings_customise_sources`):

        # 1. the field-named kwarg was ignored outright:
        Settings(trading_mode="live").trading_mode          ->  'paper'
        # 2. and even once accepted, the environment outranked it,
        #    because the two arrive under different keys:
        #    (with TRADING_MODE=paper set, as every test process has it)
        Settings(trading_mode="live").trading_mode          ->  'paper'

    GUARDRAILS.md §1.2 instructs every live-trading fence test to
    "construct explicit `Settings` objects and pass them as parameters";
    a test that did so with the field name would have exercised PAPER
    mode while believing it had proved the LIVE branch, and would have
    passed. That is a vacuous test inside the fence that guards real
    money (`app/execution/fences.py::assert_live_allowed`). Both
    spellings now mean the same thing and both outrank the environment
    (see `tests/test_settings_aliases.py`).

    Env-var loading is otherwise unchanged: these two changes widen and
    correctly rank the `__init__` keyword path, they do not narrow
    environment resolution — `TRADING_MODE=live` in the environment or
    `.env` still populates `trading_mode` exactly as before.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # Application
    app_name: str = "Polymarket Trader"
    app_version: str = "0.1.0"
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    secret_key: SecretStr = Field(default=SecretStr("change-me-in-production"))

    # Database
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/polymarket",
        alias="DATABASE_URL",
    )
    database_pool_size: int = 5
    database_max_overflow: int = 10

    # Redis
    redis_url: str = Field(
        default="redis://localhost:6379",
        alias="REDIS_URL",
    )

    # Polymarket API
    polymarket_private_key: SecretStr = Field(
        default=SecretStr(""),
        alias="POLYMARKET_PRIVATE_KEY",
    )
    polymarket_funder_address: str = Field(
        default="",
        alias="POLYMARKET_FUNDER_ADDRESS",
    )
    polymarket_api_key: SecretStr = Field(
        default=SecretStr(""),
        alias="POLYMARKET_API_KEY",
    )
    polymarket_api_secret: SecretStr = Field(
        default=SecretStr(""),
        alias="POLYMARKET_API_SECRET",
    )
    polymarket_api_passphrase: SecretStr = Field(
        default=SecretStr(""),
        alias="POLYMARKET_API_PASSPHRASE",
    )

    # CLOB API
    clob_api_url: str = "https://clob.polymarket.com"
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    chain_id: int = 137  # Polygon mainnet

    # `polymarket_market_cache_ttl_s` bounds how long
    # `app.venues.polymarket.adapter.PolymarketAdapter` may serve a
    # memoized MARKET-METADATA / event-grouping lookup (T38 F6). It
    # exists because `get_book` is FOUR HTTP requests, not one: the CLOB
    # book endpoint is keyed by `token_id`, so the outcome must first be
    # resolved through `get_market()`, which is itself `GET
    # gamma/markets?condition_ids=...` + `GET gamma/events` (the FULL
    # listing) + `GET clob/markets/{condition_id}`. Measured with a
    # counting `httpx.MockTransport`: 4 requests for one `get_book`, 16
    # for four. At `scan_top_n=200` a scan pass therefore issued ~1600
    # Polymarket requests for 400 books — 400 of them identical events
    # listings, and one duplicate market lookup per outcome — against a
    # concurrency bound whose own comment (see
    # `scan_book_fetch_concurrency`) justifies itself as a request
    # budget.
    # 60s rather than "the life of a pass" because the adapter's
    # lifetime is NOT the pass's: `app.venues.registry.get_read_adapter`
    # builds a fresh one per call in `"live"` mode, but in `"paper"`
    # mode it returns the process-wide `PaperVenueAdapter` singleton,
    # whose inner read adapter lives as long as the process. A TTL is
    # what makes the same memo safe under both lifetimes. 60s is half
    # `scan_interval_s` — long enough that one pass shares one lookup,
    # short enough that a market's status/tick data cannot be served
    # stale across passes.
    # Set to 0.0 to disable memoization entirely (every call re-fetches).
    # ORDER BOOKS ARE NEVER MEMOIZED at any TTL; this covers market
    # metadata and event grouping only.
    polymarket_market_cache_ttl_s: float = Field(
        default=60.0, alias="POLYMARKET_MARKET_CACHE_TTL_S"
    )

    # Kalshi Trade API v2 (app/venues/kalshi/, T12; PLAN.md §3 Venue facts,
    # docs.kalshi.com — pinned by the architect 2026-09-04, GUARDRAILS.md
    # §1.4 forbids re-fetching them).
    #
    # `kalshi_private_key_pem` is an RSA private key in PEM form, used to
    # sign every request (RSA-PSS/SHA256 — `app/venues/kalshi/auth.py`).
    # GUARDRAILS.md §1.3: never read, print, log, or commit it; it is a
    # `SecretStr` so an accidental `repr()`/log of `Settings` redacts it.
    kalshi_api_key_id: str = Field(default="", alias="KALSHI_API_KEY_ID")
    kalshi_private_key_pem: SecretStr = Field(
        default=SecretStr(""), alias="KALSHI_PRIVATE_KEY_PEM"
    )
    #: Production Trade API v2 base. `kalshi_env="demo"` (the DEFAULT)
    #: redirects to `KALSHI_DEMO_BASE_URL` unless this is set to something
    #: other than the production default — see `kalshi_api_base_url`.
    kalshi_base_url: str = Field(
        default="https://external-api.kalshi.com/trade-api/v2",
        alias="KALSHI_BASE_URL",
    )
    #: `"demo"` by default: an operator must opt IN to production Kalshi,
    #: the same way `trading_mode` defaults to `"paper"`.
    kalshi_env: Literal["prod", "demo"] = Field(default="demo", alias="KALSHI_ENV")

    # CORS
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    # Celery
    celery_broker_url: str = Field(
        default="redis://localhost:6379/0",
        alias="CELERY_BROKER_URL",
    )
    celery_result_backend: str = Field(
        default="redis://localhost:6379/0",
        alias="CELERY_RESULT_BACKEND",
    )

    # Costs (app/venues/fees.py, T05; PLAN.md §3 Venue facts)
    #
    # Units: `*_taker_fee_rate`/`*_maker_fee_rate` are dimensionless fee
    # rates (e.g. `0.07` for 7%), consumed by `FeeModel.fee()` as
    # `size_contracts * rate * price * (1 - price)`, in USD.
    # `polymarket_taker_fee_overrides` maps a lowercased category name to
    # a taker rate override, read as a JSON object from the environment
    # (e.g. `POLYMARKET_TAKER_FEE_OVERRIDES={"crypto": 0.08}`) — kept
    # separate from `POLYMARKET_CATEGORY_TAKER_RATES` (a code constant in
    # `app/venues/fees.py`, not a `Settings` field) so an operator can
    # override one category without redeploying.
    # `redemption_gas_usd`/`transfer_cost_usd`/`min_trade_usd` are USD;
    # `transfer_latency_hours`/`near_resolution_hours`/
    # `settlement_delay_hours`/`min_hours_for_annualization` are hours;
    # `liquidity_fraction`/`min_viable_annualized` are dimensionless
    # fractions.
    kalshi_taker_fee_rate: float = Field(default=0.07, alias="KALSHI_TAKER_FEE_RATE")
    kalshi_maker_fee_rate: float = Field(default=0.0, alias="KALSHI_MAKER_FEE_RATE")
    polymarket_taker_fee_overrides: dict[str, float] = Field(
        default_factory=dict, alias="POLYMARKET_TAKER_FEE_OVERRIDES"
    )
    redemption_gas_usd: float = Field(default=0.05, alias="REDEMPTION_GAS_USD")
    transfer_latency_hours: float = Field(default=72, alias="TRANSFER_LATENCY_HOURS")
    transfer_cost_usd: float = Field(default=5.0, alias="TRANSFER_COST_USD")
    liquidity_fraction: float = Field(default=0.02, alias="LIQUIDITY_FRACTION")
    near_resolution_hours: float = Field(default=72, alias="NEAR_RESOLUTION_HOURS")
    settlement_delay_hours: float = Field(default=24, alias="SETTLEMENT_DELAY_HOURS")
    min_hours_for_annualization: float = Field(
        default=6, alias="MIN_HOURS_FOR_ANNUALIZATION"
    )
    min_viable_annualized: float = Field(default=0.05, alias="MIN_VIABLE_ANNUALIZED")
    #: Minimum USD notional (`size_contracts * limit_price`, summed
    #: across an intent's legs) the backtest/paper fill path will
    #: attempt for one intent. Below this, `Backtester._leg_sizes`
    #: returns `None` and the intent is rejected `"below_min_size"`
    #: rather than filled as dust (`app/services/backtesting/engine.py`,
    #: preserves the pre-T08 `available < 10` floor — moved here from a
    #: bare module constant, T10 retry, GUARDRAILS.md: a floor a
    #: strategy author has no way to discover is exactly how a strategy
    #: shipped with a default `min_position_size` that could never clear
    #: it). A strategy's own `min_position_size` default must be sized
    #: so `min_position_size * (sum of the legs' asks at the LOWEST
    #: price the strategy will ever signal at)` clears this floor — see
    #: `app/strategies/binary_complement_arbitrage.py` and
    #: `app/strategies/multi_outcome_bundle_arbitrage.py`.
    min_trade_usd: float = Field(default=10.0, alias="MIN_TRADE_USD")

    # Live-trading fence (PLAN.md D13, T11; app/execution/fences.py)
    #
    # `trading_mode` defaults to (and, per GUARDRAILS.md §1.2, must stay)
    # `"paper"` in every test process — `tests/conftest.py` forces the
    # `TRADING_MODE` env var to `"paper"` via `os.environ.setdefault` at
    # import time, before this `Settings` class is ever instantiated.
    # `assert_live_allowed()` (`app/execution/fences.py`) requires BOTH
    # `trading_mode == "live"` AND `live_trading_confirmation` to equal the
    # exact phrase `"I_UNDERSTAND_REAL_MONEY"` before a live venue adapter
    # (`app/venues/polymarket/live.py::PolymarketLiveAdapter`, and Kalshi's
    # T12 equivalent) can even be constructed — neither setting alone is
    # sufficient. T13 extends the fence with a kill-switch file check and
    # per-order/per-day notional limits; these two fields are not expected
    # to change shape then.
    trading_mode: Literal["paper", "live"] = Field(
        default="paper", alias="TRADING_MODE"
    )
    live_trading_confirmation: str = Field(
        default="", alias="LIVE_TRADING_CONFIRMATION"
    )

    # Live fences, part 2 (PLAN.md D13, T13; app/execution/fences.py)
    #
    # `kill_switch_path` is a filesystem path (relative to the process's
    # current working directory, or absolute) whose mere EXISTENCE halts
    # all live order placement -- `assert_live_allowed()` checks
    # `Path(kill_switch_path).exists()` only, never the file's contents,
    # so an empty file, an unreadable one, or one full of garbage all
    # engage the switch identically; there is nothing to get wrong by
    # writing the "right" text into it, and nothing to get wrong by
    # failing to read it. Deleting the file (not editing it) disengages
    # it. An operator arms it with e.g. `touch TRADING_KILL_SWITCH` from
    # the working directory the process runs in.
    #
    # The four notional/loss limits are USD and are enforced by
    # `check_order_limits()`, which BOTH the paper and live order paths
    # call (GUARDRAILS.md: limits are exercised in paper too, not only
    # once real money is at risk) -- unlike `assert_live_allowed()`,
    # which only ever guards live-adapter construction.
    # `max_near_resolution_notional_usd` is a FOURTH, additive cap (T20,
    # PLAN.md D10(d)) that `check_order_limits()` applies only when its
    # `bucket` argument is `"near_resolution"` -- it keys on that
    # `metadata["bucket"]` tag, not on time-to-close: `check_order_limits`
    # takes a `bucket` argument directly, and
    # `OrderRouter._bucket_open_notional` is what aggregates the USD
    # already committed to the bucket (across resting orders and open
    # positions) before `_check_limits` calls it.
    #
    # COVERAGE LIMIT (read before sizing this variable to bound "all
    # near-expiry exposure" -- it does not): only
    # `app.strategies.settlement_edge.SettlementEdgeStrategy` stamps
    # `metadata["bucket"] = "near_resolution"`, and
    # `app.services.scanner.near_resolution_pass` is the one caller that
    # instantiates only that strategy. A `cross_venue_arbitrage` (or any
    # other) intent on a link resolving in, say, 6 hours carries no
    # `bucket` tag at all, so it skips this cap entirely and is bounded
    # only by `max_order_notional_usd` and `max_open_notional_usd` above.
    # (`near_resolution_hours` is a separate setting, read only by
    # `app.services.scanner.near_resolution_pass` -- to decide which
    # markets that pass scans in the first place -- and by
    # `app.api.routes.arbitrage.list_opportunities`'s `near_resolution`
    # query filter. It is not read by `app/venues/fees.py`, and it does
    # not by itself gate this notional cap.)
    kill_switch_path: str = Field(
        default="TRADING_KILL_SWITCH", alias="KILL_SWITCH_PATH"
    )
    max_order_notional_usd: float = Field(
        default=250, alias="MAX_ORDER_NOTIONAL_USD"
    )
    max_daily_loss_usd: float = Field(default=100, alias="MAX_DAILY_LOSS_USD")
    max_open_notional_usd: float = Field(
        default=1000, alias="MAX_OPEN_NOTIONAL_USD"
    )
    max_near_resolution_notional_usd: float = Field(
        default=500, alias="MAX_NEAR_RESOLUTION_NOTIONAL_USD"
    )

    # Execution / order routing (PLAN.md D4/R6, T14;
    # `app/execution/{ledger,router,reconcile}.py`)
    #
    # `paper_starting_balances` seeds `app.execution.ledger.CapitalLedger`
    # in paper mode, where there is no venue account to read a `Balance`
    # from. It is keyed by VENUE and there is deliberately no single
    # "starting capital" scalar: capital is per venue (GUARDRAILS.md
    # §1.6 / PLAN.md R6 — Kalshi funds sit in a CFTC-regulated FCM
    # account whose ACH/wire settlement is measured in DAYS,
    # `transfer_latency_hours` = 72 by default), so a cross-venue intent
    # is bounded by `min(available_polymarket, available_kalshi)` and
    # never by a total. The keys mirror `app.venues.types.VenueId`
    # exactly; they are `str`-typed rather than importing that `Literal`
    # for the same reason `app.models.trade._VENUE_VALUES` is a hand-kept
    # tuple — `app.config` has no runtime dependency on `app.venues`
    # today and keeping it that way avoids an import cycle through
    # `app/venues/__init__.py`. `_check_paper_starting_balances` below
    # rejects an unknown venue key and a negative balance at load, so a
    # typo fails loudly instead of seeding a venue that then reads as
    # having no capital.
    paper_starting_balances: dict[str, float] = Field(
        default_factory=lambda: {"polymarket": 1000.0, "kalshi": 1000.0},
        alias="PAPER_STARTING_BALANCES",
    )
    #: Seconds a locally-persisted `PENDING` order may sit with no venue
    #: record before `app.execution.reconcile.reconcile()` marks it
    #: `FAILED` with reason `no_ack`. A `PENDING` row is written BEFORE
    #: the venue call (T14 crash-safety), so "no venue record" is the
    #: expected transient state for as long as the request is in flight;
    #: this is how long that state is tolerated before it is treated as
    #: a lost request rather than a slow one.
    reconcile_grace_s: float = Field(default=120.0, alias="RECONCILE_GRACE_S")
    #: Fraction of a leg's requested size that must fill for an
    #: `atomicity="all_or_none"` intent to be considered whole. Below
    #: this on ANY leg, `OrderRouter` unwinds the legs that DID fill —
    #: see `app/execution/router.py` for why that unwind is a realized
    #: loss and not a free undo.
    all_or_none_fill_tolerance: float = Field(
        default=0.995, alias="ALL_OR_NONE_FILL_TOLERANCE"
    )
    #: How many ticks BELOW the current best bid an unwind SELL's limit
    #: is placed (and above the best ask for an unwind BUY). Not
    #: slippage the unwind pays — fills happen at the book's own level
    #: prices — but how far down the book the unwind is willing to reach
    #: before giving up and leaving a loudly-recorded naked leg. `0`
    #: would only ever hit the touch; a large value approaches a true
    #: market order walking invented depth, which is how a simulation
    #: fabricates an exit that never existed.
    unwind_slippage_ticks: int = Field(default=5, alias="UNWIND_SLIPPAGE_TICKS")

    # Opportunity scanner (PLAN.md D10, T19; app/services/{scoring,scanner}.py)
    #
    # `scan_top_n` bounds how many markets PER VENUE `app.services.scanner.
    # scan()` reads a book for and runs strategies against, ranked by the
    # best available volume proxy (`VenueMarket.raw["volume"]` — neither
    # venue payload is normalized into a `VenueMarket.volume_24h` field
    # yet, so the scanner reads the same raw key both `gamma_markets.json`
    # and Kalshi's `markets.json` fixtures already carry). Unbounded
    # scanning would mean walking a book for every open market on both
    # venues every `scan_interval_s`, most of which have no volume worth
    # scoring.
    # `scan_interval_s` is the Celery beat period for
    # `app.tasks.scanner.scan_opportunities` — see `app/tasks/__init__.py`.
    # `near_resolution_scan_interval_s` is the SEPARATE beat period for
    # `app.tasks.scanner.scan_near_resolution`, which runs
    # `app.services.scanner.near_resolution_pass` (T25). It is a distinct
    # setting rather than a reuse of `scan_interval_s` because the two
    # passes are on different clocks in BOTH senses:
    #   - What they are looking for. `scan()` hunts a transient
    #     mispricing between two live books; a settlement edge is a
    #     capital-lockup trade held for HOURS-to-DAYS until the venue
    #     actually settles (`settlement_delay_hours`), so re-deriving it
    #     every 120s discovers the same rows over and over.
    #   - What they cost to run. `scan()` is bounded by `scan_top_n`
    #     markets per venue; `near_resolution_pass` reads
    #     `list_markets(status=None)` and is bounded only by how many
    #     markets are within `near_resolution_hours` of closing, which is
    #     not a number this repo controls. A longer period is how that
    #     unbounded read load is bounded.
    # 300s is 2.5x `scan_interval_s`'s default: still well inside a
    # 24h-plus settlement window, and it keeps the two beats from landing
    # on the same second on every common multiple.
    # `max_slippage_bps` is the cushion `app.services.scoring.score()`
    # allows a leg's fill price to exceed (BUY) or undercut (SELL) its
    # limit by, in basis points of the limit price, before that fill no
    # longer counts toward `OpportunityScore.fill_confidence` — a walked
    # price at exactly the limit is not slippage-free in practice, and 0
    # bps of tolerance would report a thick, perfectly-priced book as
    # unfillable over float noise alone.
    scan_top_n: int = Field(default=200, alias="SCAN_TOP_N")
    # `scan_book_fetch_concurrency` bounds how many `get_book` calls
    # `app.services.scanner.scan()` has IN FLIGHT AT ONCE, summed across
    # every venue in `adapters` together (one shared semaphore, not one
    # per venue — see `scan()`'s docstring for why a per-venue bound
    # would not fix the problem this exists for). `scan_top_n`'s default
    # of 200 markets x 2 outcomes x 2 venues means a single pass has up
    # to 800 `get_book` calls to make. Awaited one at a time (T19's
    # original shape) that is minutes of serialized HTTP round-trips —
    # against TWO price feeds a cross-venue arbitrage signal claims are
    # simultaneous, and against request budgets now shared by THREE
    # beats (`scan_opportunities`, `scan_near_resolution`,
    # `propose_event_links`), none of which publishes a limit generous
    # enough to assume 800 back-to-back requests is safe. Unbounded
    # concurrency (`asyncio.gather` over all 800 at once) trades that
    # risk for a worse one: a burst indistinguishable from abuse to
    # whatever is rate-limiting on the other end. This default is
    # deliberately conservative rather than tuned for minimum wall time —
    # enough overlap to turn "minutes" into low single-digit seconds
    # without opening hundreds of sockets at once; lower it further if a
    # venue's actual published limit (once known) demands it.
    # WHAT THIS BOUNDS, PRECISELY (T38 F6). It bounds in-flight `get_book`
    # CALLS, and because the several HTTP requests inside one such call
    # are issued sequentially, it does also bound in-flight HTTP
    # REQUESTS at the same number — the RATE budget above holds. What it
    # never bounded is the pass's total request VOLUME. One Polymarket
    # `get_book` was four requests (the CLOB book endpoint is keyed by
    # `token_id`, so the outcome is resolved through `get_market()`
    # first, which is three more), so "800 `get_book` calls" was really
    # ~2000 requests a pass, not 800 — 1600 Polymarket plus 400 Kalshi,
    # where a Kalshi `get_book` is exactly one. It also made
    # Polymarket's leg of a pass several times longer than Kalshi's,
    # stretching the very fetch window the concurrency work exists to
    # shrink. `polymarket_market_cache_ttl_s` is the fix; this bound was
    # measuring what its comment said, but over a call count that was
    # not the request count.
    scan_book_fetch_concurrency: int = Field(
        default=20, alias="SCAN_BOOK_FETCH_CONCURRENCY"
    )
    scan_interval_s: float = Field(default=120.0, alias="SCAN_INTERVAL_S")
    near_resolution_scan_interval_s: float = Field(
        default=300.0, alias="NEAR_RESOLUTION_SCAN_INTERVAL_S"
    )
    max_slippage_bps: float = Field(default=50.0, alias="MAX_SLIPPAGE_BPS")

    # Cross-venue link proposal (PLAN.md D9, T30; app/tasks/matching.py)
    #
    # `link_proposal_interval_s` is the Celery beat period for
    # `app.tasks.matching.propose_event_links`, the periodic caller
    # `app.services.matching.propose_links` never had — before T30 the
    # ONLY caller was the manual `POST /links/propose`, so on a fresh
    # install `event_links` stayed empty forever and
    # `cross_venue_arbitrage` (which consumes ONLY `approved` links)
    # scanned nothing, permanently.
    # A THIRD interval rather than a reuse of `scan_interval_s` or
    # `near_resolution_scan_interval_s`, for the same two reasons those
    # two are separate from each other:
    #   - What it is looking for. The scans chase a mispricing between
    #     two live order books, which is gone in minutes. This pass
    #     chases the appearance of a new MARKET on a venue and the
    #     wording of its rules — a market list turns over on a scale of
    #     hours-to-days, and its output is not a trade but a row in a
    #     queue a HUMAN has to read (PLAN.md D9: nothing trades on a
    #     proposed link). Re-deriving the same proposals every two
    #     minutes would not move a single approval forward.
    #   - What it costs to run. `scan()` is bounded by `scan_top_n`
    #     markets per venue; this pass reads EVERY open market on BOTH
    #     venues and scores the blocked cross product of the two lists,
    #     which is the heaviest read-plus-compute in the repo. An hour is
    #     how that is bounded.
    # 3600s (30x `scan_interval_s`) is well under the time it takes a
    # reviewer to work a queue, so no proposal waits on the beat.
    link_proposal_interval_s: float = Field(
        default=3600.0, alias="LINK_PROPOSAL_INTERVAL_S"
    )

    # Recorded book-depth collection (PLAN.md D6/D10, T21;
    # app/models/book_snapshot.py, app/services/data_collector.py,
    # app/services/backtesting/data_replay.py)
    #
    # `book_match_window_s` bounds how STALE a recorded `BookSnapshot` may
    # be and still stand in for a replayed `PriceHistory` row's book: a
    # book observed 3 minutes before the price row is not "the book at
    # that price", it is a different market state, so `DataReplayer`
    # only attaches one within this many seconds BEFORE the row's own
    # `ts` (never after — attaching a FUTURE book would be look-ahead,
    # PLAN.md D6). Outside the window (or when no `BookSnapshot` exists
    # at all), the replayer falls back to `synthesize_book` (T07) and the
    # result is labeled `depth_source="synthetic"`/`"mixed"` accordingly.
    # `book_collection_top_n` bounds how many markets, PER VENUE,
    # `DataCollector.collect_books` fetches a book for per call — the
    # same "don't walk a book for every market nobody trades" rationale
    # `scan_top_n` documents above, ranked by the identical volume proxy
    # (`VenueMarket.raw["volume"]`, `app.services.scanner._volume`'s
    # convention).
    book_match_window_s: float = Field(default=120.0, alias="BOOK_MATCH_WINDOW_S")
    book_collection_top_n: int = Field(default=50, alias="BOOK_COLLECTION_TOP_N")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Keep the documented source precedence, with init kwargs re-keyed.

        The order returned here is pydantic-settings' own default —
        constructor arguments first (highest priority), then environment,
        `.env`, and file secrets. The ONLY change is that the init source
        is wrapped in `_AliasNormalizedInitSource`, so a constructor
        argument spelled with the field name collides with (and therefore
        beats) the same setting coming from the environment under its
        alias, instead of losing to it silently. See that class's
        docstring for why this is a money-fence concern and not a
        cosmetic one.
        """
        init_kwargs = getattr(init_settings, "init_kwargs", {})
        return (
            _AliasNormalizedInitSource(settings_cls, init_kwargs),
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )

    @field_validator("polymarket_taker_fee_overrides")
    @classmethod
    def _normalize_taker_fee_override_keys(cls, value: dict[str, float]) -> dict[str, float]:
        """Lowercase every override key once at load (DEFECT 2).

        `app.venues.fees.category_rate()` normalizes the *incoming*
        category name to lowercase before looking it up in this dict,
        but PLAN.md §3 spells categories capitalized (`Crypto`,
        `Geopolitics`) — the venue's own natural convention, and exactly
        what an operator is likely to type into
        `POLYMARKET_TAKER_FEE_OVERRIDES`. Without this, a
        `{"Geopolitics": 0.02}` override is silently discarded (the
        lookup key never matches) and the category-table default is
        used instead — a *silent* fallback that looks like a working
        override. Normalizing here, once, at settings load, means every
        reader of `settings.polymarket_taker_fee_overrides` (not just
        `category_rate()`) sees already-normalized keys, rather than
        each call site having to remember to re-normalize.

        An explicit `0.0` override is a real float value, not falsy-
        absent, and survives this unchanged — only the key casing
        changes, never the value.
        """
        return {key.strip().lower(): rate for key, rate in value.items()}

    @field_validator("paper_starting_balances")
    @classmethod
    def _check_paper_starting_balances(cls, value: dict[str, float]) -> dict[str, float]:
        """Reject an unknown/missing venue key or a negative starting balance.

        `PAPER_STARTING_BALANCES={"polymkt": 5000}` would otherwise load
        without complaint and leave `polymarket` seeded at nothing — the
        paper ledger would then refuse every Polymarket order for
        "insufficient capital" and the operator would have no way to see
        why. Venue names are normalized to lowercase first, so
        `{"Kalshi": 500}` is accepted as `kalshi`.

        EVERY venue must be present, not merely no unknown ones. A
        partial `{"polymarket": 500}` used to load happily and seed a
        ledger that had never heard of `kalshi` — and
        `CapitalLedger.reserve("kalshi", ...)` raises `UnknownVenue`,
        which is a `LedgerError` and NOT the `InsufficientCapital` the
        order router catches. A legal config could therefore throw an
        exception straight out of `OrderRouter.submit()` from between the
        placement of one leg and the recording of the next. The router
        now also treats any `LedgerError` as a rejection (defence in
        depth), but the honest place to catch this is here, at load,
        where the operator can see it.

        Args:
            value: The raw mapping as loaded from the environment/`.env`.

        Returns:
            dict[str, float]: The mapping with lowercased venue keys.

        Raises:
            ValueError: If a key is not a known venue, if a known venue
                is missing, or if a balance is negative.
        """
        normalized: dict[str, float] = {}
        for venue, amount in value.items():
            key = venue.strip().lower()
            if key not in _PAPER_LEDGER_VENUES:
                raise ValueError(
                    f"paper_starting_balances key {venue!r} is not a known venue "
                    f"(expected one of {sorted(_PAPER_LEDGER_VENUES)})"
                )
            if amount < 0:
                raise ValueError(
                    f"paper_starting_balances[{key!r}] must be >= 0, got {amount!r}"
                )
            normalized[key] = float(amount)
        missing = sorted(_PAPER_LEDGER_VENUES - set(normalized))
        if missing:
            raise ValueError(
                f"paper_starting_balances is missing {missing}; every venue must "
                "be seeded (use 0 to make a venue deliberately untradeable). An "
                "unseeded venue raises UnknownVenue out of the capital ledger "
                "rather than the InsufficientCapital the order router handles"
            )
        return normalized

    @property
    def kalshi_api_base_url(self) -> str:
        """Return the Kalshi Trade API v2 base URL for the configured env.

        PLAN.md §3 pins both bases: production
        `https://external-api.kalshi.com/trade-api/v2` and demo
        `https://external-api.demo.kalshi.co/trade-api/v2`. `kalshi_env`
        selects between them, so an operator flips ONE obvious variable
        rather than remembering to retype a URL.

        An explicitly-set `kalshi_base_url` always wins: if it differs
        from the production default, it is returned as-is regardless of
        `kalshi_env`. That keeps a deliberate override (a proxy, the
        `api.elections.kalshi.com` alternate host PLAN.md §3 also lists)
        from being silently replaced by the demo URL just because
        `kalshi_env` was left at its default.

        Returns:
            str: The base URL every `app/venues/kalshi` request is made
                against. Includes the `/trade-api/v2` path prefix, which
                is also part of what gets SIGNED (see
                `app/venues/kalshi/auth.py::sign_request`).
        """
        default_prod = type(self).model_fields["kalshi_base_url"].default
        if self.kalshi_base_url != default_prod:
            return self.kalshi_base_url
        if self.kalshi_env == "demo":
            return KALSHI_DEMO_BASE_URL
        return self.kalshi_base_url

    @property
    def async_database_url(self) -> str:
        """Get async database URL."""
        if "+asyncpg" not in self.database_url:
            return self.database_url.replace("postgresql://", "postgresql+asyncpg://")
        return self.database_url


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


settings = get_settings()
