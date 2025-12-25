"""Backtesting engine for strategy evaluation.

Provides a realistic simulation environment for testing trading
strategies against historical market data.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable
import logging

from app.strategies.base import BaseStrategy, MarketSnapshot, Signal, SignalType


logger = logging.getLogger(__name__)


class SlippageModel(str, Enum):
    """Slippage model types."""

    NONE = "none"
    FIXED = "fixed"  # Fixed percentage
    VOLUME_BASED = "volume_based"  # Based on trade size vs volume
    SPREAD_BASED = "spread_based"  # Based on bid-ask spread


@dataclass
class BacktestConfig:
    """Configuration for a backtest run.

    Attributes:
        start_date: Backtest start timestamp.
        end_date: Backtest end timestamp.
        initial_capital: Starting capital in dollars.
        fee_rate: Trading fee as decimal (e.g., 0.001 for 0.1%).
        slippage_model: Type of slippage simulation.
        slippage_value: Slippage parameter (interpretation depends on model).
        markets_filter: Optional list of market IDs to include.
        max_position_pct: Maximum position size as fraction of portfolio.
    """

    start_date: datetime
    end_date: datetime
    initial_capital: float = 10000.0
    fee_rate: float = 0.0
    slippage_model: SlippageModel = SlippageModel.FIXED
    slippage_value: float = 0.001  # 0.1% default
    markets_filter: list[str] | None = None
    max_position_pct: float = 0.20

    def __post_init__(self) -> None:
        """Validate configuration."""
        if self.start_date >= self.end_date:
            raise ValueError("start_date must be before end_date")
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if not 0 <= self.fee_rate < 1:
            raise ValueError("fee_rate must be between 0 and 1")
        if not 0 < self.max_position_pct <= 1:
            raise ValueError("max_position_pct must be between 0 and 1")


@dataclass
class Position:
    """An open position in the portfolio.

    Attributes:
        market_id: Market condition ID.
        outcome: Position outcome (YES/NO).
        token_id: Token identifier.
        entry_price: Average entry price.
        size: Position size in tokens.
        entry_time: When position was opened.
        stop_loss: Optional stop loss price.
        take_profit: Optional take profit price.
        cost_basis: Total cost to acquire position.
        metadata: Additional position metadata.
    """

    market_id: str
    outcome: str
    token_id: str
    entry_price: float
    size: float
    entry_time: datetime
    stop_loss: float | None = None
    take_profit: float | None = None
    cost_basis: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Calculate cost basis if not provided."""
        if self.cost_basis == 0.0:
            self.cost_basis = self.entry_price * self.size

    @property
    def position_id(self) -> str:
        """Unique identifier for this position."""
        return f"{self.market_id}:{self.outcome}"

    def current_value(self, current_price: float) -> float:
        """Calculate current position value."""
        return current_price * self.size

    def unrealized_pnl(self, current_price: float) -> float:
        """Calculate unrealized P&L."""
        return self.current_value(current_price) - self.cost_basis

    def unrealized_pnl_pct(self, current_price: float) -> float:
        """Calculate unrealized P&L percentage."""
        if self.cost_basis == 0:
            return 0.0
        return self.unrealized_pnl(current_price) / self.cost_basis


@dataclass
class Portfolio:
    """Portfolio state during backtest.

    Attributes:
        cash: Available cash balance.
        positions: Dictionary of open positions by position_id.
        equity_history: List of (timestamp, equity) tuples.
        realized_pnl: Total realized profit/loss.
        peak_value: Maximum portfolio value seen (for drawdown).
    """

    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    equity_history: list[tuple[datetime, float]] = field(default_factory=list)
    realized_pnl: float = 0.0
    peak_value: float = 0.0

    def __post_init__(self) -> None:
        """Initialize peak value."""
        if self.peak_value == 0.0:
            self.peak_value = self.cash

    def total_equity(self, prices: dict[str, float]) -> float:
        """Calculate total portfolio equity.

        Args:
            prices: Dict mapping position_id to current price.

        Returns:
            Total portfolio value (cash + positions).
        """
        positions_value = sum(
            pos.current_value(prices.get(pos.position_id, pos.entry_price))
            for pos in self.positions.values()
        )
        return self.cash + positions_value

    def update_equity_history(
        self,
        timestamp: datetime,
        prices: dict[str, float],
    ) -> None:
        """Record current equity in history."""
        equity = self.total_equity(prices)
        self.equity_history.append((timestamp, equity))

        # Update peak for drawdown tracking
        if equity > self.peak_value:
            self.peak_value = equity

    def current_drawdown(self, prices: dict[str, float]) -> float:
        """Calculate current drawdown from peak."""
        equity = self.total_equity(prices)
        if self.peak_value == 0:
            return 0.0
        return (self.peak_value - equity) / self.peak_value


@dataclass
class TradeRecord:
    """Record of an executed trade.

    Attributes:
        timestamp: When trade was executed.
        market_id: Market condition ID.
        outcome: YES or NO.
        token_id: Token traded.
        side: BUY or SELL.
        price: Execution price.
        size: Number of tokens.
        fee: Trading fee paid.
        slippage: Slippage applied.
        pnl: Realized P&L (for closing trades).
        signal_confidence: Original signal confidence.
        metadata: Additional trade metadata.
    """

    timestamp: datetime
    market_id: str
    outcome: str
    token_id: str
    side: str
    price: float
    size: float
    fee: float = 0.0
    slippage: float = 0.0
    pnl: float | None = None
    signal_confidence: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def total_cost(self) -> float:
        """Total cost including fees."""
        return (self.price * self.size) + self.fee

    @property
    def is_profitable(self) -> bool:
        """Check if trade was profitable."""
        return self.pnl is not None and self.pnl > 0


@dataclass
class BacktestResult:
    """Results from a completed backtest.

    Attributes:
        config: Backtest configuration used.
        strategy_name: Name of strategy tested.
        strategy_config: Strategy configuration.
        start_time: When backtest started.
        end_time: When backtest completed.
        initial_capital: Starting capital.
        final_value: Final portfolio value.
        total_return: Total return (final/initial - 1).
        equity_curve: List of (timestamp, equity) tuples.
        trades: List of all executed trades.
        positions_final: Open positions at end.
        snapshots_processed: Number of market snapshots processed.
        signals_generated: Number of signals generated.
        errors: List of any errors encountered.
    """

    config: BacktestConfig
    strategy_name: str
    strategy_config: dict[str, Any]
    start_time: datetime
    end_time: datetime
    initial_capital: float
    final_value: float
    total_return: float
    equity_curve: list[tuple[datetime, float]]
    trades: list[TradeRecord]
    positions_final: dict[str, Position]
    snapshots_processed: int = 0
    signals_generated: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total_trades(self) -> int:
        """Total number of trades executed."""
        return len(self.trades)

    @property
    def winning_trades(self) -> int:
        """Number of profitable trades."""
        return sum(1 for t in self.trades if t.is_profitable)

    @property
    def losing_trades(self) -> int:
        """Number of losing trades."""
        return sum(1 for t in self.trades if t.pnl is not None and t.pnl < 0)


class Backtester:
    """Backtesting engine for strategy evaluation.

    Simulates trading a strategy against historical market data
    with realistic execution, slippage, and fees.

    Example:
        ```python
        config = BacktestConfig(
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 6, 1),
            initial_capital=10000,
        )
        strategy = get_strategy("catalyst_momentum")
        backtester = Backtester(config, strategy)
        result = await backtester.run(data_replayer)
        ```
    """

    def __init__(
        self,
        config: BacktestConfig,
        strategy: BaseStrategy,
        progress_callback: Callable[[float], None] | None = None,
    ) -> None:
        """Initialize backtester.

        Args:
            config: Backtest configuration.
            strategy: Strategy to test.
            progress_callback: Optional callback for progress updates (0-1).
        """
        self.config = config
        self.strategy = strategy
        self.progress_callback = progress_callback

        # Initialize portfolio
        self.portfolio = Portfolio(cash=config.initial_capital)

        # Tracking
        self.trades: list[TradeRecord] = []
        self.errors: list[str] = []
        self.snapshots_processed = 0
        self.signals_generated = 0
        self._current_prices: dict[str, float] = {}

    async def run(self, data_source: Any) -> BacktestResult:
        """Run the backtest.

        Args:
            data_source: DataReplayer or async iterator of MarketSnapshot.

        Returns:
            BacktestResult with complete backtest outcomes.
        """
        start_time = datetime.utcnow()

        # Reset strategy
        self.strategy.reset()
        await self.strategy.initialize()

        # Process each snapshot
        try:
            async for snapshot in data_source:
                # Check market filter
                if self.config.markets_filter:
                    if snapshot.market_id not in self.config.markets_filter:
                        continue

                await self._process_snapshot(snapshot)
                self.snapshots_processed += 1

                # Progress callback
                if self.progress_callback:
                    total_duration = (
                        self.config.end_date - self.config.start_date
                    ).total_seconds()
                    elapsed = (
                        snapshot.timestamp - self.config.start_date
                    ).total_seconds()
                    progress = min(elapsed / total_duration, 1.0)
                    self.progress_callback(progress)

        except Exception as e:
            logger.error(f"Backtest error: {e}")
            self.errors.append(str(e))

        # Close remaining positions at end
        if self.portfolio.positions:
            await self._close_all_positions(self.config.end_date)

        # Calculate final equity
        final_value = self.portfolio.total_equity(self._current_prices)
        total_return = (final_value / self.config.initial_capital) - 1

        # Cleanup
        await self.strategy.cleanup()

        return BacktestResult(
            config=self.config,
            strategy_name=self.strategy.name,
            strategy_config=self.strategy.config,
            start_time=start_time,
            end_time=datetime.utcnow(),
            initial_capital=self.config.initial_capital,
            final_value=final_value,
            total_return=total_return,
            equity_curve=self.portfolio.equity_history.copy(),
            trades=self.trades.copy(),
            positions_final=self.portfolio.positions.copy(),
            snapshots_processed=self.snapshots_processed,
            signals_generated=self.signals_generated,
            errors=self.errors.copy(),
        )

    async def _process_snapshot(self, snapshot: MarketSnapshot) -> None:
        """Process a single market snapshot.

        Args:
            snapshot: Market data snapshot to process.
        """
        # Update current prices
        position_id = f"{snapshot.market_id}:YES"
        self._current_prices[position_id] = snapshot.yes_price
        position_id_no = f"{snapshot.market_id}:NO"
        self._current_prices[position_id_no] = snapshot.no_price

        # Check for position exits (stop loss, take profit)
        await self._check_position_exits(snapshot)

        # Generate signal from strategy
        signal = self.strategy.on_market_data(snapshot)

        if signal and signal.type != SignalType.HOLD:
            self.signals_generated += 1

            # Calculate position size
            portfolio_value = self.portfolio.total_equity(self._current_prices)
            positions_dict = {
                pid: {
                    "market_id": p.market_id,
                    "size": p.size,
                    "entry_price": p.entry_price,
                    "metadata": p.metadata,
                }
                for pid, p in self.portfolio.positions.items()
            }

            size = self.strategy.calculate_position_size(
                signal, portfolio_value, positions_dict
            )

            if size > 0:
                signal.size = size
                await self._execute_signal(signal, snapshot)

        # Update equity history
        self.portfolio.update_equity_history(
            snapshot.timestamp,
            self._current_prices,
        )

    async def _execute_signal(
        self,
        signal: Signal,
        snapshot: MarketSnapshot,
    ) -> None:
        """Execute a trading signal.

        Args:
            signal: Signal to execute.
            snapshot: Current market snapshot.
        """
        position_id = f"{signal.market_id}:{signal.outcome}"

        if signal.type == SignalType.BUY:
            await self._open_position(signal, snapshot)
        elif signal.type == SignalType.SELL:
            # Check if we have a position to close
            if position_id in self.portfolio.positions:
                await self._close_position(position_id, snapshot.timestamp, signal.price)
            else:
                # Short selling not implemented - would need to open NO position
                logger.debug(f"No position to close for {position_id}")

    async def _open_position(
        self,
        signal: Signal,
        snapshot: MarketSnapshot,
    ) -> None:
        """Open a new position or add to existing.

        Args:
            signal: Buy signal.
            snapshot: Current market snapshot.
        """
        position_id = f"{signal.market_id}:{signal.outcome}"

        # Apply slippage to get execution price
        exec_price = self._apply_slippage(signal.price, "buy", snapshot)

        # Calculate fee
        fee = signal.size * exec_price * self.config.fee_rate

        # Check if we have enough cash
        total_cost = (signal.size * exec_price) + fee

        # Apply max position constraint
        portfolio_value = self.portfolio.total_equity(self._current_prices)
        max_position_value = portfolio_value * self.config.max_position_pct

        if total_cost > max_position_value:
            # Scale down position
            scale = max_position_value / total_cost
            signal.size *= scale
            total_cost = (signal.size * exec_price) + fee

        if total_cost > self.portfolio.cash:
            # Not enough cash - scale to available
            available = self.portfolio.cash * 0.99  # Keep small buffer
            if available < 10:  # Minimum trade size
                return
            scale = available / total_cost
            signal.size *= scale
            fee = signal.size * exec_price * self.config.fee_rate
            total_cost = (signal.size * exec_price) + fee

        # Calculate tokens received
        tokens = signal.size / exec_price

        # Update or create position
        if position_id in self.portfolio.positions:
            # Add to existing position (average entry)
            pos = self.portfolio.positions[position_id]
            total_tokens = pos.size + tokens
            avg_price = (pos.cost_basis + total_cost) / total_tokens
            pos.size = total_tokens
            pos.entry_price = avg_price
            pos.cost_basis += total_cost
        else:
            # Create new position
            self.portfolio.positions[position_id] = Position(
                market_id=signal.market_id,
                outcome=signal.outcome,
                token_id=signal.token_id,
                entry_price=exec_price,
                size=tokens,
                entry_time=snapshot.timestamp,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                cost_basis=total_cost,
                metadata=signal.metadata,
            )

        # Deduct cash
        self.portfolio.cash -= total_cost

        # Record trade
        trade = TradeRecord(
            timestamp=snapshot.timestamp,
            market_id=signal.market_id,
            outcome=signal.outcome,
            token_id=signal.token_id,
            side="BUY",
            price=exec_price,
            size=tokens,
            fee=fee,
            slippage=exec_price - signal.price,
            signal_confidence=signal.confidence,
            metadata=signal.metadata,
        )
        self.trades.append(trade)

        # Notify strategy
        self.strategy.on_trade_executed({
            "market_id": signal.market_id,
            "token_id": signal.token_id,
            "side": "BUY",
            "price": exec_price,
            "size": tokens,
            "fee": fee,
            "timestamp": snapshot.timestamp,
        })

    async def _close_position(
        self,
        position_id: str,
        timestamp: datetime,
        price: float,
    ) -> None:
        """Close an existing position.

        Args:
            position_id: Position to close.
            timestamp: Current timestamp.
            price: Exit price.
        """
        if position_id not in self.portfolio.positions:
            return

        pos = self.portfolio.positions[position_id]

        # Apply slippage
        exec_price = self._apply_slippage(price, "sell", None)

        # Calculate proceeds
        proceeds = pos.size * exec_price

        # Calculate fee
        fee = proceeds * self.config.fee_rate

        # Calculate P&L
        net_proceeds = proceeds - fee
        pnl = net_proceeds - pos.cost_basis

        # Update portfolio
        self.portfolio.cash += net_proceeds
        self.portfolio.realized_pnl += pnl
        del self.portfolio.positions[position_id]

        # Record trade
        trade = TradeRecord(
            timestamp=timestamp,
            market_id=pos.market_id,
            outcome=pos.outcome,
            token_id=pos.token_id,
            side="SELL",
            price=exec_price,
            size=pos.size,
            fee=fee,
            slippage=exec_price - price,
            pnl=pnl,
            metadata=pos.metadata,
        )
        self.trades.append(trade)

        # Notify strategy
        self.strategy.on_position_closed(
            {
                "market_id": pos.market_id,
                "token_id": pos.token_id,
                "entry_price": pos.entry_price,
                "exit_price": exec_price,
                "size": pos.size,
                "entry_time": pos.entry_time,
                "exit_time": timestamp,
            },
            pnl,
        )

    def _apply_slippage(
        self,
        price: float,
        side: str,
        snapshot: MarketSnapshot | None,
    ) -> float:
        """Apply slippage to execution price.

        Args:
            price: Original price.
            side: 'buy' or 'sell'.
            snapshot: Market snapshot for volume/spread based slippage.

        Returns:
            Adjusted execution price.
        """
        if self.config.slippage_model == SlippageModel.NONE:
            return price

        slippage = 0.0

        if self.config.slippage_model == SlippageModel.FIXED:
            slippage = self.config.slippage_value

        elif self.config.slippage_model == SlippageModel.SPREAD_BASED:
            if snapshot and snapshot.spread:
                slippage = snapshot.spread / 2
            else:
                slippage = self.config.slippage_value

        elif self.config.slippage_model == SlippageModel.VOLUME_BASED:
            # Higher slippage for larger orders relative to volume
            if snapshot and snapshot.volume_24h > 0:
                # Simplified model: slippage increases with trade size
                slippage = self.config.slippage_value * 2
            else:
                slippage = self.config.slippage_value

        # Apply direction
        if side == "buy":
            return min(price + slippage, 0.99)  # Cap at 0.99
        else:
            return max(price - slippage, 0.01)  # Floor at 0.01

    async def _check_position_exits(self, snapshot: MarketSnapshot) -> None:
        """Check for stop loss and take profit exits.

        Args:
            snapshot: Current market snapshot.
        """
        positions_to_close: list[tuple[str, float, str]] = []

        for position_id, pos in self.portfolio.positions.items():
            if pos.market_id != snapshot.market_id:
                continue

            # Get current price for this position
            current_price = (
                snapshot.yes_price if pos.outcome == "YES" else snapshot.no_price
            )

            # Check stop loss
            if pos.stop_loss is not None:
                if pos.outcome == "YES" and current_price <= pos.stop_loss:
                    positions_to_close.append((position_id, current_price, "stop_loss"))
                elif pos.outcome == "NO" and current_price <= pos.stop_loss:
                    positions_to_close.append((position_id, current_price, "stop_loss"))

            # Check take profit
            if pos.take_profit is not None:
                if pos.outcome == "YES" and current_price >= pos.take_profit:
                    positions_to_close.append((position_id, current_price, "take_profit"))
                elif pos.outcome == "NO" and current_price >= pos.take_profit:
                    positions_to_close.append((position_id, current_price, "take_profit"))

        # Close positions that hit exits
        for position_id, exit_price, reason in positions_to_close:
            logger.debug(f"Closing {position_id} due to {reason} at {exit_price}")
            await self._close_position(position_id, snapshot.timestamp, exit_price)

    async def _close_all_positions(self, timestamp: datetime) -> None:
        """Close all open positions at end of backtest.

        Args:
            timestamp: Closing timestamp.
        """
        position_ids = list(self.portfolio.positions.keys())

        for position_id in position_ids:
            pos = self.portfolio.positions[position_id]
            # Use last known price
            price = self._current_prices.get(position_id, pos.entry_price)
            await self._close_position(position_id, timestamp, price)
