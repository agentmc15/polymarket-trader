BEGIN;

CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL, 
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

-- Running upgrade  -> 001

CREATE TABLE price_history (
    id SERIAL NOT NULL, 
    market_id VARCHAR(66) NOT NULL, 
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL, 
    yes_price FLOAT NOT NULL, 
    no_price FLOAT NOT NULL, 
    yes_bid FLOAT, 
    yes_ask FLOAT, 
    no_bid FLOAT, 
    no_ask FLOAT, 
    spread FLOAT, 
    volume FLOAT DEFAULT '0.0' NOT NULL, 
    volume_24h FLOAT DEFAULT '0.0' NOT NULL, 
    open_interest FLOAT, 
    PRIMARY KEY (id)
);

CREATE INDEX ix_price_history_market_id ON price_history (market_id);

CREATE INDEX ix_price_history_timestamp ON price_history (timestamp);

CREATE INDEX ix_price_history_market_timestamp ON price_history (market_id, timestamp);

CREATE INDEX ix_price_history_timestamp_desc ON price_history (timestamp DESC);

CREATE UNIQUE INDEX uq_price_history_market_timestamp ON price_history (market_id, timestamp);

DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                -- TimescaleDB refuses to convert a table carrying ANY unique
                -- index that omits the partitioning column, and `price_history_pkey`
                -- is on `id` alone. Without this DROP the conversion fails with
                -- "cannot create a unique index without the column timestamp" and
                -- takes the whole migration chain down with it -- verified
                -- against timescale/timescaledb:latest-pg15.
                --
                -- Dropping it is safe rather than a concession: row uniqueness
                -- is already enforced by the natural key
                -- `uq_price_history_market_timestamp (market_id, timestamp)`,
                -- created above, which DOES contain the partitioning column. The
                -- surrogate PK adds nothing here, `id` keeps its sequence and
                -- stays unique, and no foreign key references this table.
                ALTER TABLE price_history DROP CONSTRAINT IF EXISTS price_history_pkey;
                PERFORM create_hypertable('price_history', 'timestamp', if_not_exists => TRUE);
            END IF;
        END $$;;

CREATE TYPE tradeside AS ENUM ('BUY', 'SELL');

CREATE TYPE tradeoutcome AS ENUM ('YES', 'NO');

CREATE TABLE trade_history (
    id SERIAL NOT NULL, 
    market_id VARCHAR(66) NOT NULL, 
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL, 
    side tradeside NOT NULL, 
    outcome tradeoutcome NOT NULL, 
    price FLOAT NOT NULL, 
    size FLOAT NOT NULL, 
    maker_address VARCHAR(42), 
    taker_address VARCHAR(42), 
    tx_hash VARCHAR(66), 
    PRIMARY KEY (id), 
    UNIQUE (tx_hash)
);

CREATE INDEX ix_trade_history_market_id ON trade_history (market_id);

CREATE INDEX ix_trade_history_timestamp ON trade_history (timestamp);

CREATE INDEX ix_trade_history_market_timestamp ON trade_history (market_id, timestamp);

CREATE INDEX ix_trade_history_timestamp_desc ON trade_history (timestamp DESC);

CREATE INDEX ix_trade_history_maker_address ON trade_history (maker_address);

CREATE INDEX ix_trade_history_taker_address ON trade_history (taker_address);

CREATE INDEX ix_trade_history_maker_taker ON trade_history (maker_address, taker_address);

CREATE TABLE tracked_traders (
    id SERIAL NOT NULL, 
    address VARCHAR(42) NOT NULL, 
    name VARCHAR(200), 
    total_pnl FLOAT DEFAULT '0.0' NOT NULL, 
    win_rate FLOAT DEFAULT '0.0' NOT NULL, 
    total_trades INTEGER DEFAULT '0' NOT NULL, 
    is_active BOOLEAN DEFAULT 'true' NOT NULL, 
    copy_multiplier FLOAT DEFAULT '1.0' NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    UNIQUE (address)
);

CREATE INDEX ix_tracked_traders_address ON tracked_traders (address);

CREATE INDEX ix_tracked_traders_is_active ON tracked_traders (is_active);

CREATE INDEX ix_tracked_traders_pnl ON tracked_traders (total_pnl);

CREATE INDEX ix_tracked_traders_win_rate ON tracked_traders (win_rate);

CREATE INDEX ix_tracked_traders_active_pnl ON tracked_traders (is_active, total_pnl);

CREATE TYPE backtestrunstatus AS ENUM ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED');

CREATE TABLE backtest_runs (
    id SERIAL NOT NULL, 
    strategy_name VARCHAR(200) NOT NULL, 
    strategy_config JSONB DEFAULT '{}' NOT NULL, 
    start_date TIMESTAMP WITH TIME ZONE NOT NULL, 
    end_date TIMESTAMP WITH TIME ZONE NOT NULL, 
    initial_capital FLOAT NOT NULL, 
    fee_rate FLOAT DEFAULT '0.0' NOT NULL, 
    status backtestrunstatus DEFAULT 'PENDING' NOT NULL, 
    final_value FLOAT, 
    total_return FLOAT, 
    sharpe_ratio FLOAT, 
    max_drawdown FLOAT, 
    win_rate FLOAT, 
    total_trades INTEGER DEFAULT '0' NOT NULL, 
    equity_curve JSONB DEFAULT '[]' NOT NULL, 
    trades_list JSONB DEFAULT '[]' NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    completed_at TIMESTAMP WITH TIME ZONE, 
    PRIMARY KEY (id)
);

CREATE INDEX ix_backtest_runs_strategy_name ON backtest_runs (strategy_name);

CREATE INDEX ix_backtest_runs_status ON backtest_runs (status);

CREATE INDEX ix_backtest_runs_created_at ON backtest_runs (created_at);

CREATE INDEX ix_backtest_runs_strategy_status ON backtest_runs (strategy_name, status);

CREATE INDEX ix_backtest_runs_sharpe ON backtest_runs (sharpe_ratio);

CREATE INDEX ix_backtest_runs_total_return ON backtest_runs (total_return);

INSERT INTO alembic_version (version_num) VALUES ('001') RETURNING alembic_version.version_num;

-- Running upgrade 001 -> 002

DROP INDEX ix_tracked_traders_active_pnl;

DROP INDEX ix_tracked_traders_win_rate;

DROP INDEX ix_tracked_traders_pnl;

DROP INDEX ix_tracked_traders_is_active;

DROP INDEX ix_tracked_traders_address;

DROP TABLE tracked_traders;

DROP TABLE IF EXISTS trader_follows;

DROP TABLE IF EXISTS traders;

UPDATE alembic_version SET version_num='002' WHERE alembic_version.version_num = '001';

-- Running upgrade 002 -> 003

ALTER TABLE backtest_runs ADD COLUMN report JSONB DEFAULT '{}' NOT NULL;

UPDATE alembic_version SET version_num='003' WHERE alembic_version.version_num = '002';

-- Running upgrade 003 -> 004

CREATE TYPE orderside AS ENUM ('BUY', 'SELL');

CREATE TABLE markets (
    id SERIAL NOT NULL, 
    venue VARCHAR(16) NOT NULL, 
    condition_id VARCHAR(66) NOT NULL, 
    question_id VARCHAR(66), 
    question TEXT NOT NULL, 
    description TEXT, 
    category VARCHAR(100), 
    token_ids JSONB NOT NULL, 
    outcomes JSONB NOT NULL, 
    is_active BOOLEAN NOT NULL, 
    is_resolved BOOLEAN NOT NULL, 
    resolution_outcome VARCHAR(50), 
    end_date TIMESTAMP WITH TIME ZONE, 
    resolved_at TIMESTAMP WITH TIME ZONE, 
    volume_24h FLOAT NOT NULL, 
    total_volume FLOAT NOT NULL, 
    liquidity FLOAT NOT NULL, 
    outcome_prices JSONB NOT NULL, 
    source_url TEXT, 
    icon_url TEXT, 
    extra_data JSONB NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT uq_markets_venue_condition_id UNIQUE (venue, condition_id), 
    CONSTRAINT ck_markets_venue_valid CHECK (venue IN ('polymarket', 'kalshi'))
);

CREATE INDEX ix_markets_category ON markets (category);

CREATE INDEX ix_markets_is_active ON markets (is_active);

CREATE INDEX ix_markets_venue ON markets (venue);

CREATE INDEX ix_markets_end_date ON markets (end_date);

CREATE INDEX ix_markets_condition_id ON markets (condition_id);

CREATE TABLE market_prices (
    id SERIAL NOT NULL, 
    market_id INTEGER NOT NULL, 
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL, 
    outcome VARCHAR(50) NOT NULL, 
    open FLOAT NOT NULL, 
    high FLOAT NOT NULL, 
    low FLOAT NOT NULL, 
    close FLOAT NOT NULL, 
    volume FLOAT NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(market_id) REFERENCES markets (id)
);

CREATE INDEX ix_market_prices_market_id ON market_prices (market_id);

CREATE INDEX ix_market_prices_timestamp ON market_prices (timestamp);

CREATE INDEX ix_market_prices_market_timestamp ON market_prices (market_id, timestamp);

CREATE TYPE strategytype AS ENUM ('ARBITRAGE', 'MOMENTUM', 'MEAN_REVERSION', 'MARKET_MAKING', 'CUSTOM');

CREATE TABLE strategies (
    id SERIAL NOT NULL, 
    name VARCHAR(100) NOT NULL, 
    description TEXT, 
    strategy_type strategytype NOT NULL, 
    parameters JSONB NOT NULL, 
    default_parameters JSONB NOT NULL, 
    max_position_size FLOAT NOT NULL, 
    max_daily_loss FLOAT NOT NULL, 
    stop_loss_pct FLOAT, 
    take_profit_pct FLOAT, 
    is_active BOOLEAN NOT NULL, 
    is_backtested BOOLEAN NOT NULL, 
    total_trades INTEGER NOT NULL, 
    win_rate FLOAT NOT NULL, 
    avg_profit FLOAT NOT NULL, 
    sharpe_ratio FLOAT, 
    max_drawdown FLOAT, 
    module_path VARCHAR(255), 
    class_name VARCHAR(100), 
    extra_data JSONB NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    UNIQUE (name)
);

CREATE TYPE ordertype AS ENUM ('GTC', 'GTD', 'FOK');

CREATE TYPE orderstatus AS ENUM ('PENDING', 'OPEN', 'FILLED', 'PARTIALLY_FILLED', 'CANCELLED', 'EXPIRED', 'FAILED');

CREATE TABLE orders (
    id SERIAL NOT NULL, 
    order_id VARCHAR(100), 
    market_id INTEGER NOT NULL, 
    venue VARCHAR(16) NOT NULL, 
    client_order_id VARCHAR(64) NOT NULL, 
    intent_id VARCHAR(64), 
    token_id VARCHAR(100) NOT NULL, 
    outcome VARCHAR(50) NOT NULL, 
    side orderside NOT NULL, 
    order_type ordertype NOT NULL, 
    status orderstatus NOT NULL, 
    mode VARCHAR(8) NOT NULL, 
    price FLOAT NOT NULL, 
    size FLOAT NOT NULL, 
    filled_size FLOAT NOT NULL, 
    remaining_size FLOAT NOT NULL, 
    expires_at TIMESTAMP WITH TIME ZONE, 
    filled_at TIMESTAMP WITH TIME ZONE, 
    tx_hash VARCHAR(66), 
    error_message TEXT, 
    extra_data JSONB NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(market_id) REFERENCES markets (id), 
    UNIQUE (client_order_id), 
    CONSTRAINT ck_orders_venue_valid CHECK (venue IN ('polymarket', 'kalshi')), 
    CONSTRAINT ck_orders_mode_valid CHECK (mode IN ('paper', 'live'))
);

CREATE INDEX ix_orders_market_id ON orders (market_id);

CREATE INDEX ix_orders_venue ON orders (venue);

CREATE INDEX ix_orders_intent_id ON orders (intent_id);

CREATE INDEX ix_orders_mode ON orders (mode);

CREATE INDEX ix_orders_status ON orders (status);

CREATE INDEX ix_orders_created_at ON orders (created_at);

CREATE UNIQUE INDEX ix_orders_order_id ON orders (order_id);

CREATE TABLE trades (
    id SERIAL NOT NULL, 
    trade_id VARCHAR(100) NOT NULL, 
    order_id INTEGER, 
    market_id INTEGER NOT NULL, 
    venue VARCHAR(16) NOT NULL, 
    token_id VARCHAR(100) NOT NULL, 
    outcome VARCHAR(50) NOT NULL, 
    side orderside NOT NULL, 
    price FLOAT NOT NULL, 
    size FLOAT NOT NULL, 
    fee FLOAT NOT NULL, 
    liquidity VARCHAR(8), 
    mode VARCHAR(8) NOT NULL, 
    maker_address VARCHAR(42), 
    taker_address VARCHAR(42), 
    tx_hash VARCHAR(66), 
    block_number INTEGER, 
    executed_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    extra_data JSONB NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(order_id) REFERENCES orders (id), 
    FOREIGN KEY(market_id) REFERENCES markets (id), 
    CONSTRAINT ck_trades_venue_valid CHECK (venue IN ('polymarket', 'kalshi')), 
    CONSTRAINT ck_trades_mode_valid CHECK (mode IN ('paper', 'live')), 
    CONSTRAINT ck_trades_liquidity_valid CHECK (liquidity IN ('maker', 'taker') OR liquidity IS NULL)
);

CREATE INDEX ix_trades_market_id ON trades (market_id);

CREATE INDEX ix_trades_venue ON trades (venue);

CREATE INDEX ix_trades_mode ON trades (mode);

CREATE INDEX ix_trades_executed_at ON trades (executed_at);

CREATE INDEX ix_trades_maker_address ON trades (maker_address);

CREATE INDEX ix_trades_taker_address ON trades (taker_address);

CREATE UNIQUE INDEX ix_trades_trade_id ON trades (trade_id);

CREATE TABLE positions (
    id SERIAL NOT NULL, 
    market_id INTEGER NOT NULL, 
    venue VARCHAR(16) NOT NULL, 
    mode VARCHAR(8) NOT NULL, 
    intent_id VARCHAR(64), 
    token_id VARCHAR(100) NOT NULL, 
    outcome VARCHAR(50) NOT NULL, 
    size FLOAT NOT NULL, 
    avg_entry_price FLOAT NOT NULL, 
    total_cost FLOAT NOT NULL, 
    current_price FLOAT NOT NULL, 
    current_value FLOAT NOT NULL, 
    unrealized_pnl FLOAT NOT NULL, 
    unrealized_pnl_pct FLOAT NOT NULL, 
    realized_pnl FLOAT NOT NULL, 
    hold_to_resolution BOOLEAN NOT NULL, 
    opened_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    closed_at TIMESTAMP WITH TIME ZONE, 
    settled_at TIMESTAMP WITH TIME ZONE, 
    settlement_outcome VARCHAR(50), 
    extra_data JSONB NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(market_id) REFERENCES markets (id), 
    CONSTRAINT ck_positions_venue_valid CHECK (venue IN ('polymarket', 'kalshi')), 
    CONSTRAINT ck_positions_mode_valid CHECK (mode IN ('paper', 'live'))
);

CREATE INDEX ix_positions_market_id ON positions (market_id);

CREATE INDEX ix_positions_venue ON positions (venue);

CREATE INDEX ix_positions_mode ON positions (mode);

CREATE INDEX ix_positions_intent_id ON positions (intent_id);

CREATE INDEX ix_positions_token_id ON positions (token_id);

CREATE INDEX ix_positions_opened_at ON positions (opened_at);

CREATE TYPE backteststatus AS ENUM ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED');

CREATE TABLE backtests (
    id SERIAL NOT NULL, 
    strategy_id INTEGER NOT NULL, 
    name VARCHAR(200), 
    start_date TIMESTAMP WITH TIME ZONE NOT NULL, 
    end_date TIMESTAMP WITH TIME ZONE NOT NULL, 
    initial_capital FLOAT NOT NULL, 
    parameters JSONB NOT NULL, 
    status backteststatus NOT NULL, 
    progress FLOAT NOT NULL, 
    error_message TEXT, 
    final_capital FLOAT, 
    total_return FLOAT, 
    total_return_pct FLOAT, 
    annualized_return FLOAT, 
    sharpe_ratio FLOAT, 
    sortino_ratio FLOAT, 
    max_drawdown FLOAT, 
    max_drawdown_pct FLOAT, 
    volatility FLOAT, 
    total_trades INTEGER NOT NULL, 
    winning_trades INTEGER NOT NULL, 
    losing_trades INTEGER NOT NULL, 
    win_rate FLOAT, 
    avg_win FLOAT, 
    avg_loss FLOAT, 
    profit_factor FLOAT, 
    started_at TIMESTAMP WITH TIME ZONE, 
    completed_at TIMESTAMP WITH TIME ZONE, 
    duration_seconds FLOAT, 
    equity_curve JSONB NOT NULL, 
    extra_data JSONB NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(strategy_id) REFERENCES strategies (id)
);

CREATE INDEX ix_backtests_strategy_id ON backtests (strategy_id);

CREATE INDEX ix_backtests_status ON backtests (status);

CREATE INDEX ix_backtests_created_at ON backtests (created_at);

CREATE TABLE backtest_trades (
    id SERIAL NOT NULL, 
    backtest_id INTEGER NOT NULL, 
    market_condition_id VARCHAR(66) NOT NULL, 
    token_id VARCHAR(100) NOT NULL, 
    side orderside NOT NULL, 
    entry_price FLOAT NOT NULL, 
    exit_price FLOAT, 
    size FLOAT NOT NULL, 
    fee FLOAT NOT NULL, 
    entry_time TIMESTAMP WITH TIME ZONE NOT NULL, 
    exit_time TIMESTAMP WITH TIME ZONE, 
    pnl FLOAT, 
    pnl_pct FLOAT, 
    signal_name VARCHAR(100), 
    signal_strength FLOAT, 
    extra_data JSONB NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(backtest_id) REFERENCES backtests (id)
);

CREATE INDEX ix_backtest_trades_backtest_id ON backtest_trades (backtest_id);

CREATE INDEX ix_backtest_trades_entry_time ON backtest_trades (entry_time);

CREATE TABLE intents (
    id VARCHAR(64) NOT NULL, 
    kind VARCHAR(16) NOT NULL, 
    strategy VARCHAR(100) NOT NULL, 
    mode VARCHAR(8) NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    status VARCHAR(16) DEFAULT 'pending' NOT NULL, 
    legs JSONB NOT NULL, 
    score JSONB NOT NULL, 
    extra_data JSONB NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT ck_intents_kind_valid CHECK (kind IN ('single', 'complement', 'bundle', 'cross_venue')), 
    CONSTRAINT ck_intents_mode_valid CHECK (mode IN ('paper', 'live')), 
    CONSTRAINT ck_intents_status_valid CHECK (status IN ('pending', 'executed', 'rejected', 'expired'))
);

CREATE INDEX ix_intents_created_at ON intents (created_at);

CREATE INDEX ix_intents_mode ON intents (mode);

CREATE INDEX ix_intents_status ON intents (status);

UPDATE alembic_version SET version_num='004' WHERE alembic_version.version_num = '003';

-- Running upgrade 004 -> 005

CREATE TABLE event_links (
    id SERIAL NOT NULL, 
    venue_a VARCHAR(16) NOT NULL, 
    market_a VARCHAR(128) NOT NULL, 
    venue_b VARCHAR(16) NOT NULL, 
    market_b VARCHAR(128) NOT NULL, 
    outcome_map JSONB NOT NULL, 
    confidence FLOAT NOT NULL, 
    evidence JSONB NOT NULL, 
    status VARCHAR(16) DEFAULT 'proposed' NOT NULL, 
    reviewed_by VARCHAR(100), 
    reviewed_at TIMESTAMP WITH TIME ZONE, 
    notes TEXT, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT uq_event_links_pair UNIQUE (venue_a, market_a, venue_b, market_b), 
    CONSTRAINT ck_event_links_status_valid CHECK (status IN ('proposed', 'approved', 'rejected')), 
    CONSTRAINT ck_event_links_venue_a_valid CHECK (venue_a IN ('polymarket', 'kalshi')), 
    CONSTRAINT ck_event_links_venue_b_valid CHECK (venue_b IN ('polymarket', 'kalshi')), 
    CONSTRAINT ck_event_links_confidence_range CHECK (confidence >= 0.0 AND confidence <= 1.0)
);

CREATE INDEX ix_event_links_status ON event_links (status);

CREATE INDEX ix_event_links_venue_a_market_a ON event_links (venue_a, market_a);

CREATE INDEX ix_event_links_venue_b_market_b ON event_links (venue_b, market_b);

UPDATE alembic_version SET version_num='005' WHERE alembic_version.version_num = '004';

-- Running upgrade 005 -> 006

CREATE TABLE book_snapshots (
    id SERIAL NOT NULL, 
    venue VARCHAR(16) NOT NULL, 
    market_id VARCHAR(128) NOT NULL, 
    outcome VARCHAR(50) NOT NULL, 
    ts TIMESTAMP WITH TIME ZONE NOT NULL, 
    bids JSONB NOT NULL, 
    asks JSONB NOT NULL, 
    tick_size FLOAT NOT NULL, 
    min_size FLOAT NOT NULL, 
    depth_source VARCHAR(16) DEFAULT 'recorded' NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT uq_book_snapshots_venue_market_outcome_ts UNIQUE (venue, market_id, outcome, ts), 
    CONSTRAINT ck_book_snapshots_venue_valid CHECK (venue IN ('polymarket', 'kalshi')), 
    CONSTRAINT ck_book_snapshots_depth_source_valid CHECK (depth_source IN ('recorded', 'synthetic')), 
    CONSTRAINT ck_book_snapshots_tick_size_range CHECK (tick_size > 0.0 AND tick_size <= 1.0), 
    CONSTRAINT ck_book_snapshots_min_size_nonnegative CHECK (min_size >= 0.0)
);

CREATE INDEX ix_book_snapshots_ts ON book_snapshots (ts);

CREATE INDEX ix_book_snapshots_venue_market_outcome ON book_snapshots (venue, market_id, outcome);

DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                -- TimescaleDB refuses to convert a table carrying ANY unique
                -- index that omits the partitioning column, and `book_snapshots_pkey`
                -- is on `id` alone. Without this DROP the conversion fails with
                -- "cannot create a unique index without the column ts" and
                -- takes the whole migration chain down with it -- verified
                -- against timescale/timescaledb:latest-pg15.
                --
                -- Dropping it is safe rather than a concession: row uniqueness
                -- is already enforced by the natural key
                -- `uq_book_snapshots_venue_market_outcome_ts (venue, market_id, outcome, ts)`,
                -- created above, which DOES contain the partitioning column. The
                -- surrogate PK adds nothing here, `id` keeps its sequence and
                -- stays unique, and no foreign key references this table.
                ALTER TABLE book_snapshots DROP CONSTRAINT IF EXISTS book_snapshots_pkey;
                PERFORM create_hypertable('book_snapshots', 'ts', if_not_exists => TRUE);
            END IF;
        END $$;;

UPDATE alembic_version SET version_num='006' WHERE alembic_version.version_num = '005';

-- Running upgrade 006 -> 007

DELETE FROM book_snapshots
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM book_snapshots
            GROUP BY venue, market_id, ts, 
        CASE
            WHEN lower(btrim(outcome)) IN ('yes', 'no')
                THEN upper(btrim(outcome))
            ELSE lower(btrim(outcome))
        END

        );;

UPDATE book_snapshots
        SET outcome = 
        CASE
            WHEN lower(btrim(outcome)) IN ('yes', 'no')
                THEN upper(btrim(outcome))
            ELSE lower(btrim(outcome))
        END

        WHERE outcome <> 
        CASE
            WHEN lower(btrim(outcome)) IN ('yes', 'no')
                THEN upper(btrim(outcome))
            ELSE lower(btrim(outcome))
        END
;;

UPDATE alembic_version SET version_num='007' WHERE alembic_version.version_num = '006';

-- Running upgrade 007 -> 008

ALTER TABLE book_snapshots ADD COLUMN volume FLOAT;

ALTER TABLE book_snapshots ADD COLUMN taker_fee_rate FLOAT;

ALTER TABLE book_snapshots ADD COLUMN maker_rebate_rate FLOAT;

ALTER TABLE book_snapshots ADD COLUMN volume_lifetime FLOAT;

ALTER TABLE book_snapshots ADD COLUMN observed_at TIMESTAMP WITH TIME ZONE;

ALTER TABLE book_snapshots ADD COLUMN fee_source VARCHAR(32);

ALTER TABLE book_snapshots ADD COLUMN maker_fee_rate FLOAT;

UPDATE alembic_version SET version_num='008' WHERE alembic_version.version_num = '007';

-- Running upgrade 008 -> 009

CREATE TABLE selection_membership (
    id SERIAL NOT NULL, 
    venue VARCHAR(16) NOT NULL, 
    market_id VARCHAR(128) NOT NULL, 
    selected BOOLEAN NOT NULL, 
    last_selected_at TIMESTAMP WITH TIME ZONE, 
    last_deselected_at TIMESTAMP WITH TIME ZONE, 
    PRIMARY KEY (id), 
    CONSTRAINT uq_selection_membership_venue_market UNIQUE (venue, market_id), 
    CONSTRAINT ck_selection_membership_venue_valid CHECK (venue IN ('polymarket', 'kalshi'))
);

UPDATE alembic_version SET version_num='009' WHERE alembic_version.version_num = '008';

COMMIT;

