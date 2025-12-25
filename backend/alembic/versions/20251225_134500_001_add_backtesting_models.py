"""Add backtesting models.

Revision ID: 001
Revises:
Create Date: 2025-12-25 13:45:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create price_history table (will be converted to TimescaleDB hypertable)
    op.create_table(
        "price_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("market_id", sa.String(66), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("yes_price", sa.Float(), nullable=False),
        sa.Column("no_price", sa.Float(), nullable=False),
        sa.Column("yes_bid", sa.Float(), nullable=True),
        sa.Column("yes_ask", sa.Float(), nullable=True),
        sa.Column("no_bid", sa.Float(), nullable=True),
        sa.Column("no_ask", sa.Float(), nullable=True),
        sa.Column("spread", sa.Float(), nullable=True),
        sa.Column("volume", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("volume_24h", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("open_interest", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_price_history_market_id", "price_history", ["market_id"])
    op.create_index("ix_price_history_timestamp", "price_history", ["timestamp"])
    op.create_index(
        "ix_price_history_market_timestamp", "price_history", ["market_id", "timestamp"]
    )
    op.create_index(
        "ix_price_history_timestamp_desc", "price_history", [sa.text("timestamp DESC")]
    )
    op.create_index(
        "uq_price_history_market_timestamp",
        "price_history",
        ["market_id", "timestamp"],
        unique=True,
    )

    # Convert price_history to TimescaleDB hypertable
    # This requires TimescaleDB extension to be installed
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                PERFORM create_hypertable('price_history', 'timestamp', if_not_exists => TRUE);
            END IF;
        END $$;
        """
    )

    # Create trade_history table
    op.create_table(
        "trade_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("market_id", sa.String(66), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "side",
            sa.Enum("BUY", "SELL", name="tradeside"),
            nullable=False,
        ),
        sa.Column(
            "outcome",
            sa.Enum("YES", "NO", name="tradeoutcome"),
            nullable=False,
        ),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("size", sa.Float(), nullable=False),
        sa.Column("maker_address", sa.String(42), nullable=True),
        sa.Column("taker_address", sa.String(42), nullable=True),
        sa.Column("tx_hash", sa.String(66), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tx_hash"),
    )
    op.create_index("ix_trade_history_market_id", "trade_history", ["market_id"])
    op.create_index("ix_trade_history_timestamp", "trade_history", ["timestamp"])
    op.create_index(
        "ix_trade_history_market_timestamp", "trade_history", ["market_id", "timestamp"]
    )
    op.create_index(
        "ix_trade_history_timestamp_desc", "trade_history", [sa.text("timestamp DESC")]
    )
    op.create_index("ix_trade_history_maker_address", "trade_history", ["maker_address"])
    op.create_index("ix_trade_history_taker_address", "trade_history", ["taker_address"])
    op.create_index(
        "ix_trade_history_maker_taker",
        "trade_history",
        ["maker_address", "taker_address"],
    )

    # Create tracked_traders table
    op.create_table(
        "tracked_traders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("address", sa.String(42), nullable=False),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("total_pnl", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("win_rate", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("total_trades", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("copy_multiplier", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("address"),
    )
    op.create_index("ix_tracked_traders_address", "tracked_traders", ["address"])
    op.create_index("ix_tracked_traders_is_active", "tracked_traders", ["is_active"])
    op.create_index("ix_tracked_traders_pnl", "tracked_traders", ["total_pnl"])
    op.create_index("ix_tracked_traders_win_rate", "tracked_traders", ["win_rate"])
    op.create_index(
        "ix_tracked_traders_active_pnl", "tracked_traders", ["is_active", "total_pnl"]
    )

    # Create backtest_runs table
    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("strategy_name", sa.String(200), nullable=False),
        sa.Column("strategy_config", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("start_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("initial_capital", sa.Float(), nullable=False),
        sa.Column("fee_rate", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED",
                name="backtestrunstatus"
            ),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("final_value", sa.Float(), nullable=True),
        sa.Column("total_return", sa.Float(), nullable=True),
        sa.Column("sharpe_ratio", sa.Float(), nullable=True),
        sa.Column("max_drawdown", sa.Float(), nullable=True),
        sa.Column("win_rate", sa.Float(), nullable=True),
        sa.Column("total_trades", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("equity_curve", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("trades_list", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_backtest_runs_strategy_name", "backtest_runs", ["strategy_name"])
    op.create_index("ix_backtest_runs_status", "backtest_runs", ["status"])
    op.create_index("ix_backtest_runs_created_at", "backtest_runs", ["created_at"])
    op.create_index(
        "ix_backtest_runs_strategy_status",
        "backtest_runs",
        ["strategy_name", "status"],
    )
    op.create_index("ix_backtest_runs_sharpe", "backtest_runs", ["sharpe_ratio"])
    op.create_index("ix_backtest_runs_total_return", "backtest_runs", ["total_return"])


def downgrade() -> None:
    # Drop backtest_runs
    op.drop_index("ix_backtest_runs_total_return", table_name="backtest_runs")
    op.drop_index("ix_backtest_runs_sharpe", table_name="backtest_runs")
    op.drop_index("ix_backtest_runs_strategy_status", table_name="backtest_runs")
    op.drop_index("ix_backtest_runs_created_at", table_name="backtest_runs")
    op.drop_index("ix_backtest_runs_status", table_name="backtest_runs")
    op.drop_index("ix_backtest_runs_strategy_name", table_name="backtest_runs")
    op.drop_table("backtest_runs")
    op.execute("DROP TYPE IF EXISTS backtestrunstatus")

    # Drop tracked_traders
    op.drop_index("ix_tracked_traders_active_pnl", table_name="tracked_traders")
    op.drop_index("ix_tracked_traders_win_rate", table_name="tracked_traders")
    op.drop_index("ix_tracked_traders_pnl", table_name="tracked_traders")
    op.drop_index("ix_tracked_traders_is_active", table_name="tracked_traders")
    op.drop_index("ix_tracked_traders_address", table_name="tracked_traders")
    op.drop_table("tracked_traders")

    # Drop trade_history
    op.drop_index("ix_trade_history_maker_taker", table_name="trade_history")
    op.drop_index("ix_trade_history_taker_address", table_name="trade_history")
    op.drop_index("ix_trade_history_maker_address", table_name="trade_history")
    op.drop_index("ix_trade_history_timestamp_desc", table_name="trade_history")
    op.drop_index("ix_trade_history_market_timestamp", table_name="trade_history")
    op.drop_index("ix_trade_history_timestamp", table_name="trade_history")
    op.drop_index("ix_trade_history_market_id", table_name="trade_history")
    op.drop_table("trade_history")
    op.execute("DROP TYPE IF EXISTS tradeoutcome")
    op.execute("DROP TYPE IF EXISTS tradeside")

    # Drop price_history (hypertable will be dropped with the table)
    op.drop_index("uq_price_history_market_timestamp", table_name="price_history")
    op.drop_index("ix_price_history_timestamp_desc", table_name="price_history")
    op.drop_index("ix_price_history_market_timestamp", table_name="price_history")
    op.drop_index("ix_price_history_timestamp", table_name="price_history")
    op.drop_index("ix_price_history_market_id", table_name="price_history")
    op.drop_table("price_history")
