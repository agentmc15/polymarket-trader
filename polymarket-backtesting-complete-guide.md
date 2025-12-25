# Polymarket Trading Application: Complete Backtesting & Strategy Guide

A comprehensive guide for building a production-grade backtesting system with all 10 proven Polymarket trading strategies.

---

## Table of Contents

1. [Overview](#overview)
2. [All 10 Trading Strategies](#all-10-trading-strategies)
3. [Database Setup](#database-setup)
4. [Data Collection](#data-collection)
5. [Backtesting Engine](#backtesting-engine)
6. [Strategy Implementation](#strategy-implementation)
7. [API & Frontend](#api--frontend)
8. [Running Backtests](#running-backtests)

---

## Overview

This guide implements a complete backtesting framework with all 10 strategies from Datawallet's analysis:

| # | Strategy | Edge/Core Focus | Risk Profile | Automation |
|---|----------|-----------------|--------------|------------|
| 1 | Binary Complement Arbitrage | Buy YES+NO shares priced below $1 total | Near-Zero | High |
| 2 | Multi-Outcome Bundle Arbitrage | Buy full outcome set under $1 combined | Near-Zero | High |
| 3 | Catalyst Momentum | Rapid repricing after breaking news | Moderate/High | Moderate |
| 4 | Settlement Edge | Trade on resolution criteria, not headline | Low/Moderate | Low |
| 5 | Term-Structure Spreads | Arb same market across different expiry dates | Moderate | Moderate |
| 6 | Correlation Hedging | Offset risk using related, correlated markets | Moderate | Moderate/High |
| 7 | Cross-Platform Arbitrage | Trade same market with pricing gaps across platforms | Near-Zero | High |
| 8 | Favorite Compounder | Grind high-probability bets with reliable returns | Low (Tail risk) | Moderate |
| 9 | "No" Bias Exploit | Fade overpriced YES in phrase-based markets | Low | Moderate |
| 10 | Whale Copy-Trading | Follow top wallets before price moves | Moderate | Moderate/High |

---

## All 10 Trading Strategies

### Strategy 1: Binary Complement Arbitrage (YES + NO < 1)

**Concept:** Scan for markets where YES_ask + NO_ask < $1.00. Buying both guarantees profit at resolution.

**Example:** Fed rate cut market has YES @ 27¢ and NO @ 71¢ = 98¢ total. Buy both for guaranteed 2¢ profit.

```python
class BinaryComplementArbitrageStrategy(BaseStrategy):
    """
    Risk-free arbitrage when YES + NO prices sum to less than $1.
    """
    DEFAULT_CONFIG = {
        "min_edge": 0.02,      # Minimum 2 cents profit
        "min_liquidity": 100,  # Minimum shares available
        "include_fees": True,
        "fee_rate": 0.01,
    }
    
    async def on_market_data(self, snapshot: MarketSnapshot) -> Optional[Signal]:
        total_cost = snapshot.yes_ask + snapshot.no_ask
        
        if self.config["include_fees"]:
            fee_cost = total_cost * self.config["fee_rate"] * 2
            effective_cost = total_cost + fee_cost
        else:
            effective_cost = total_cost
        
        edge = 1.0 - effective_cost
        
        if edge >= self.config["min_edge"]:
            return Signal(
                type=SignalType.BUY,
                outcome="YES" if snapshot.yes_ask <= snapshot.no_ask else "NO",
                price=min(snapshot.yes_ask, snapshot.no_ask),
                confidence=min(edge / 0.05, 1.0),
                metadata={"edge": edge, "paired_trade": True}
            )
        return None
```

---

### Strategy 2: Multi-Outcome Bundle Arbitrage

**Concept:** In multi-outcome markets (e.g., "Best Picture Winner"), sum cheapest ask for ALL outcomes. If total < $1, buy the bundle.

**Example:** Oscar nominees sum to 97¢. Buy one share of each = guaranteed $1 payout = 3¢ profit.

```python
class MultiOutcomeBundleArbitrageStrategy(BaseStrategy):
    """
    Bundle arbitrage in multi-outcome markets.
    """
    DEFAULT_CONFIG = {
        "min_edge": 0.03,
        "max_outcomes": 20,
    }
    
    def __init__(self, config):
        super().__init__(config)
        self.market_outcomes = {}  # event_id -> list of outcomes
    
    async def on_market_data(self, snapshot: MarketSnapshot) -> Optional[Signal]:
        event_id = self._find_event(snapshot.market_id)
        if not event_id:
            return None
        
        # Calculate total bundle cost
        total_cost = sum(outcome["ask"] for outcome in self.market_outcomes[event_id])
        edge = 1.0 - total_cost
        
        if edge >= self.config["min_edge"]:
            cheapest = min(self.market_outcomes[event_id], key=lambda x: x["ask"])
            return Signal(
                type=SignalType.BUY,
                market_id=cheapest["market_id"],
                outcome="YES",
                confidence=min(edge / 0.05, 1.0),
                metadata={"bundle_trade": True, "num_outcomes": len(self.market_outcomes[event_id])}
            )
        return None
```

---

### Strategy 3: Catalyst Momentum

**Concept:** After breaking news, prices gap before all traders update. Buy the initial move, sell into follow-through.

**Example:** ETH ETF rumor causes odds to jump 35% → 50%. Buy at 38¢, sell at 48-50¢ as liquidity catches up.

```python
class CatalystMomentumStrategy(BaseStrategy):
    """
    Trade momentum after news catalysts.
    """
    DEFAULT_CONFIG = {
        "price_change_threshold": 0.05,     # 5% price move to trigger
        "volume_spike_multiplier": 3.0,     # 3x average volume
        "take_profit_pct": 0.10,
        "stop_loss_pct": 0.05,
        "lookback_periods": 20,
    }
    
    async def on_market_data(self, snapshot: MarketSnapshot) -> Optional[Signal]:
        # Calculate price change
        prev_price = self.last_prices.get(snapshot.market_id, snapshot.yes_price)
        price_change = (snapshot.yes_price - prev_price) / prev_price
        
        # Check volume spike
        avg_volume = sum(self.volume_history[snapshot.market_id]) / len(self.volume_history[snapshot.market_id])
        volume_ratio = snapshot.volume / avg_volume
        
        if abs(price_change) >= self.config["price_change_threshold"] and \
           volume_ratio >= self.config["volume_spike_multiplier"]:
            
            direction = "YES" if price_change > 0 else "NO"
            entry_price = snapshot.yes_price if direction == "YES" else snapshot.no_price
            
            return Signal(
                type=SignalType.BUY,
                outcome=direction,
                price=entry_price,
                stop_loss=entry_price * (1 - self.config["stop_loss_pct"]),
                take_profit=entry_price * (1 + self.config["take_profit_pct"]),
                metadata={"price_change": price_change, "volume_ratio": volume_ratio}
            )
        return None
```

---

### Strategy 4: Settlement Edge

**Concept:** Trade the resolution criteria, NOT the headline. Markets misprice when traders focus on "headline truth" vs specific settlement rules.

**Example:** "Government shutdown in 2025?" - Rules require OPM announcement. Price political chaos at 30%, but OPM trigger is only 18%. Buy NO.

```python
class SettlementEdgeStrategy(BaseStrategy):
    """
    Trade based on resolution rules vs market price.
    """
    DEFAULT_CONFIG = {
        "min_edge": 0.10,
        "confidence_discount": 0.8,
    }
    
    def __init__(self, config):
        super().__init__(config)
        self.probability_estimates = {}  # market_id -> estimated true probability
    
    def set_probability_estimate(self, market_id: str, probability: float, reasoning: str = ""):
        """Set manual probability based on rule analysis."""
        self.probability_estimates[market_id] = probability
    
    async def on_market_data(self, snapshot: MarketSnapshot) -> Optional[Signal]:
        if snapshot.market_id not in self.probability_estimates:
            return None
        
        estimated_prob = self.probability_estimates[snapshot.market_id]
        market_price = snapshot.yes_price
        edge = estimated_prob - market_price
        
        if abs(edge) >= self.config["min_edge"]:
            if edge > 0:
                return Signal(type=SignalType.BUY, outcome="YES", price=market_price)
            else:
                return Signal(type=SignalType.BUY, outcome="NO", price=snapshot.no_price)
        return None
```

---

### Strategy 5: Term-Structure Spreads

**Concept:** Compare same market across different expiry dates. Spot mispriced tail probabilities in the term structure.

**Example:** "BTC > $100k" - Sept @ 46% vs Nov @ 48%. Curve is too flat. Buy Nov, sell Sept as convergence trade.

```python
class TermStructureSpreadsStrategy(BaseStrategy):
    """
    Trade term structure mispricing across expiry dates.
    """
    DEFAULT_CONFIG = {
        "min_spread": 0.05,
        "max_date_diff_days": 90,
        "volatility_model": "sqrt_time",
    }
    
    def __init__(self, config):
        super().__init__(config)
        self.market_groups = {}  # group_id -> list of {market_id, end_date}
    
    async def on_market_data(self, snapshot: MarketSnapshot) -> Optional[Signal]:
        group_id = self._find_group(snapshot.market_id)
        if not group_id:
            return None
        
        # Compare prices across expiries
        near_term, far_term = self._get_term_pair(group_id)
        actual_spread = far_term["price"] - near_term["price"]
        fair_spread = self._calculate_fair_spread(near_term, far_term)
        
        mispricing = abs(actual_spread - fair_spread)
        
        if mispricing >= self.config["min_spread"]:
            # Buy underpriced leg
            if actual_spread > fair_spread:
                return Signal(type=SignalType.BUY, market_id=far_term["market_id"])
            else:
                return Signal(type=SignalType.BUY, market_id=near_term["market_id"])
        return None
```

---

### Strategy 6: Correlation Hedging

**Concept:** Use correlated markets to hedge and isolate relative value. Profit from basis reversion, not direction.

**Example:** Pair "Fed rate cut 2025?" with near-term meeting probabilities. If year contract is cheap vs meetings, buy year, hedge with meeting NOs.

```python
class CorrelationHedgingStrategy(BaseStrategy):
    """
    Relative value trading across correlated markets.
    """
    DEFAULT_CONFIG = {
        "min_correlation": 0.7,
        "lookback_periods": 50,
        "z_score_threshold": 2.0,
        "exit_z_score": 0.5,
    }
    
    async def on_market_data(self, snapshot: MarketSnapshot) -> Optional[Signal]:
        for market_a, market_b in self.correlation_pairs:
            correlation = self._calculate_correlation(market_a, market_b)
            if correlation < self.config["min_correlation"]:
                continue
            
            # Calculate spread z-score
            hedge_ratio = self._calculate_hedge_ratio(market_a, market_b)
            spread = self.prices[market_a] - hedge_ratio * self.prices[market_b]
            z_score = (spread - self.spread_mean) / self.spread_std
            
            if abs(z_score) >= self.config["z_score_threshold"]:
                long_market = market_b if z_score > 0 else market_a
                return Signal(
                    type=SignalType.BUY,
                    market_id=long_market,
                    metadata={"z_score": z_score, "hedge_ratio": hedge_ratio, "hedged_trade": True}
                )
        return None
```

---

### Strategy 7: Cross-Platform Arbitrage

**Concept:** Find price discrepancies between platforms (Polymarket vs Kalshi). Buy YES on cheap platform, NO on expensive.

**Example:** BTC > $95k - Polymarket 45¢, Kalshi 52¢. Buy YES on Polymarket, NO on Kalshi = 7.5% risk-free return.

```python
class CrossPlatformArbitrageStrategy(BaseStrategy):
    """
    Arbitrage across prediction market platforms.
    """
    DEFAULT_CONFIG = {
        "min_edge": 0.03,
        "platforms": ["polymarket", "kalshi"],
        "fee_rates": {"polymarket": 0.01, "kalshi": 0.01},
    }
    
    async def on_market_data(self, snapshot: MarketSnapshot) -> Optional[Signal]:
        local_id = self._find_local_id(snapshot.market_id)
        
        # Get prices from all platforms
        for p1, p2 in itertools.combinations(self.config["platforms"], 2):
            yes_cost = self.prices[p1]["yes"]
            no_cost = self.prices[p2]["no"]
            total = yes_cost + no_cost
            
            # Calculate edge after fees
            fees = yes_cost * self.config["fee_rates"][p1] + no_cost * self.config["fee_rates"][p2]
            edge = 1.0 - total - fees
            
            if edge >= self.config["min_edge"]:
                return Signal(
                    type=SignalType.BUY,
                    outcome="YES",
                    price=yes_cost,
                    metadata={
                        "cross_platform_trade": True,
                        "buy_platform": p1,
                        "hedge_platform": p2,
                        "edge": edge
                    }
                )
        return None
```

---

### Strategy 8: Favorite Compounder

**Concept:** Bet on high-probability (>90%) outcomes where real-world data suggests near-certainty. Grind consistent small gains.

**Example:** "Fed cut in December?" at 5¢ (5% chance). Data shows rates will hold. Buy NO at 95¢, collect 5.2% in 72 hours.

```python
class FavoriteCompounderStrategy(BaseStrategy):
    """
    Compound gains from high-probability outcomes.
    """
    DEFAULT_CONFIG = {
        "min_probability": 0.85,
        "max_probability": 0.98,
        "min_edge": 0.03,
        "max_days_to_expiry": 30,
        "black_swan_buffer": 0.05,
    }
    
    async def on_market_data(self, snapshot: MarketSnapshot) -> Optional[Signal]:
        # Identify favorite
        favorite_prob = max(snapshot.yes_price, snapshot.no_price)
        favorite = "YES" if snapshot.yes_price >= snapshot.no_price else "NO"
        
        # Check probability range
        if not (self.config["min_probability"] <= favorite_prob <= self.config["max_probability"]):
            return None
        
        # Check days to expiry
        days_to_expiry = (snapshot.end_date - snapshot.timestamp).days
        if days_to_expiry > self.config["max_days_to_expiry"]:
            return None
        
        # Estimate true probability (or use research)
        true_prob = self._estimate_true_probability(snapshot, favorite)
        edge = true_prob - favorite_prob
        
        if edge >= self.config["min_edge"]:
            expected_return = edge / favorite_prob
            return Signal(
                type=SignalType.BUY,
                outcome=favorite,
                price=favorite_prob,
                confidence=true_prob,
                metadata={"edge": edge, "expected_return": expected_return}
            )
        return None
```

---

### Strategy 9: "No" Bias Exploit

**Concept:** "Will X say Y?" markets overprice YES due to retail excitement. Unless speaker has history of using that phrase, NO wins.

**Example:** "Will Trump say 'Crypto' at bill signing?" at 30¢. Analysis of last 10 events shows he never used the word. Buy NO at 70¢.

```python
class NoBiasExploitStrategy(BaseStrategy):
    """
    Fade overpriced YES in mention/phrase markets.
    """
    DEFAULT_CONFIG = {
        "mention_keywords": ["will say", "will mention", "will use the word"],
        "max_yes_price": 0.40,
        "min_edge": 0.08,
        "default_base_rate": 0.85,  # Default NO probability
    }
    
    async def on_market_data(self, snapshot: MarketSnapshot) -> Optional[Signal]:
        # Check if mention market
        if not self._is_mention_market(snapshot.question):
            return None
        
        if snapshot.yes_price > self.config["max_yes_price"]:
            return None
        
        # Get research or use default
        research = self.market_research.get(snapshot.market_id)
        if research:
            true_yes_prob = research["historical_rate"]
        else:
            true_yes_prob = 1 - self.config["default_base_rate"]
        
        true_no_prob = 1 - true_yes_prob
        edge = true_no_prob - snapshot.no_price
        
        if edge >= self.config["min_edge"]:
            return Signal(
                type=SignalType.BUY,
                outcome="NO",
                price=snapshot.no_price,
                metadata={"edge": edge, "research": research}
            )
        return None
```

---

### Strategy 10: Whale Copy-Trading

**Concept:** Monitor successful wallets (high PnL, high win rate) and copy their high-conviction trades.

**Example:** Top trader "ImJustKen" buys $50k of "RFK drops out" at 15¢. Copy immediately. Two days later, press conference announced, price hits 85¢.

```python
class WhaleCopyTradingStrategy(BaseStrategy):
    """
    Copy trades from successful whale wallets.
    """
    DEFAULT_CONFIG = {
        "min_pnl": 10000,
        "min_win_rate": 0.55,
        "min_trades": 50,
        "copy_delay_seconds": 30,
        "size_multiplier": 0.25,
        "conviction_threshold": 1000,
    }
    
    def add_tracked_whale(self, address: str, pnl: float, win_rate: float, total_trades: int):
        """Add whale to tracking list after validation."""
        if pnl >= self.config["min_pnl"] and \
           win_rate >= self.config["min_win_rate"] and \
           total_trades >= self.config["min_trades"]:
            self.tracked_whales[address] = {
                "pnl": pnl,
                "win_rate": win_rate,
                "copy_multiplier": self.config["size_multiplier"]
            }
    
    def record_whale_trade(self, address: str, market_id: str, side: str, 
                           outcome: str, price: float, size: float, timestamp: datetime):
        """Record whale trade for potential copying."""
        if address not in self.tracked_whales:
            return
        if size < self.config["conviction_threshold"]:
            return
        
        self.pending_copies.append({
            "whale_address": address,
            "market_id": market_id,
            "outcome": outcome,
            "price": price,
            "size": size,
            "copy_after": timestamp + timedelta(seconds=self.config["copy_delay_seconds"])
        })
    
    async def on_market_data(self, snapshot: MarketSnapshot) -> Optional[Signal]:
        # Check for pending copies
        ready_copies = [c for c in self.pending_copies 
                       if c["copy_after"] <= snapshot.timestamp 
                       and c["market_id"] == snapshot.market_id]
        
        if ready_copies:
            copy = ready_copies[-1]  # Most recent
            whale = self.tracked_whales[copy["whale_address"]]
            
            return Signal(
                type=SignalType.BUY,
                outcome=copy["outcome"],
                price=snapshot.yes_price if copy["outcome"] == "YES" else snapshot.no_price,
                size=copy["size"] * whale["copy_multiplier"],
                confidence=whale["win_rate"],
                metadata={"whale_address": copy["whale_address"], "whale_pnl": whale["pnl"]}
            )
        return None
```

---

## Database Setup

### Models

```python
# backend/app/models/market.py

class Market(Base):
    __tablename__ = "markets"
    
    id = Column(String(66), primary_key=True)  # condition_id
    question = Column(Text, nullable=False)
    outcome_yes_token_id = Column(String(100))
    outcome_no_token_id = Column(String(100))
    status = Column(String(20))  # active, closed, resolved
    end_date = Column(DateTime)
    resolution_rules = Column(Text)


class PriceHistory(Base):
    """TimescaleDB hypertable for time-series prices."""
    __tablename__ = "price_history"
    
    market_id = Column(String(66), ForeignKey("markets.id"))
    timestamp = Column(DateTime, nullable=False)
    yes_price = Column(Float)
    no_price = Column(Float)
    yes_bid = Column(Float)
    yes_ask = Column(Float)
    volume = Column(Float)


class TradeHistory(Base):
    __tablename__ = "trade_history"
    
    market_id = Column(String(66), ForeignKey("markets.id"))
    timestamp = Column(DateTime)
    side = Column(String(4))  # BUY/SELL
    outcome = Column(String(3))  # YES/NO
    price = Column(Float)
    size = Column(Float)
    maker_address = Column(String(42))
    taker_address = Column(String(42))


class TrackedTrader(Base):
    """For whale copy-trading."""
    __tablename__ = "tracked_traders"
    
    address = Column(String(42), primary_key=True)
    total_pnl = Column(Float)
    win_rate = Column(Float)
    total_trades = Column(Integer)
    is_active = Column(Boolean, default=True)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    
    id = Column(String(36), primary_key=True)
    strategy_name = Column(String(100))
    strategy_config = Column(JSON)
    start_date = Column(DateTime)
    end_date = Column(DateTime)
    initial_capital = Column(Float)
    status = Column(String(20))  # pending, running, completed, failed
    total_return = Column(Float)
    sharpe_ratio = Column(Float)
    max_drawdown = Column(Float)
    equity_curve = Column(JSON)
    trades_list = Column(JSON)
```

### TimescaleDB Setup

```sql
-- Convert to hypertables for efficient time-series queries
SELECT create_hypertable('price_history', 'timestamp', chunk_time_interval => INTERVAL '1 day');
SELECT create_hypertable('trade_history', 'timestamp', chunk_time_interval => INTERVAL '1 day');

-- Create continuous aggregate for hourly data
CREATE MATERIALIZED VIEW price_history_hourly
WITH (timescaledb.continuous) AS
SELECT 
    market_id,
    time_bucket('1 hour', timestamp) AS bucket,
    first(yes_price, timestamp) AS open,
    max(yes_price) AS high,
    min(yes_price) AS low,
    last(yes_price, timestamp) AS close,
    sum(volume) AS volume
FROM price_history
GROUP BY market_id, bucket;
```

---

## Data Collection

```python
# backend/app/services/data_collector.py

class DataCollector:
    GAMMA_URL = "https://gamma-api.polymarket.com"
    CLOB_URL = "https://clob.polymarket.com"
    
    async def sync_markets(self):
        """Sync all markets from Gamma API."""
        markets = await self.gamma_client.get("/markets")
        for m in markets:
            await self.db.upsert(Market(
                id=m["conditionId"],
                question=m["question"],
                outcome_yes_token_id=m["clobTokenIds"][0],
                outcome_no_token_id=m["clobTokenIds"][1],
            ))
    
    async def collect_price_snapshot(self, market_id: str):
        """Get current orderbook and save price snapshot."""
        book = await self.clob_client.get(f"/book?token_id={market_id}")
        
        yes_bid = book["bids"][0][0] if book["bids"] else 0
        yes_ask = book["asks"][0][0] if book["asks"] else 1
        
        await self.db.insert(PriceHistory(
            market_id=market_id,
            timestamp=datetime.utcnow(),
            yes_price=(yes_bid + yes_ask) / 2,
            no_price=1 - (yes_bid + yes_ask) / 2,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
        ))
    
    async def update_leaderboard(self):
        """Update tracked traders from leaderboard."""
        leaderboard = await self.data_client.get("/leaderboard?limit=200")
        for trader in leaderboard:
            await self.db.upsert(TrackedTrader(
                address=trader["address"],
                total_pnl=trader["pnl"],
                win_rate=trader.get("winRate", 0),
                total_trades=trader["numTrades"],
            ))
```

---

## Backtesting Engine

```python
# backend/app/services/backtesting/engine.py

@dataclass
class BacktestConfig:
    start_date: datetime
    end_date: datetime
    initial_capital: float = 10000.0
    fee_rate: float = 0.01
    slippage_pct: float = 0.001
    max_position_pct: float = 0.25


@dataclass
class Portfolio:
    cash: float
    positions: Dict[str, Position]
    equity_history: List[Tuple[datetime, float]]
    realized_pnl: float = 0.0


class Backtester:
    def __init__(self, strategy: BaseStrategy, config: BacktestConfig):
        self.strategy = strategy
        self.config = config
    
    async def run(self, data_stream: AsyncIterator[MarketSnapshot]) -> BacktestResult:
        portfolio = Portfolio(cash=self.config.initial_capital, positions={}, equity_history=[])
        trades = []
        
        async for snapshot in data_stream:
            # Check exits (stop loss, take profit)
            await self._check_exits(portfolio, snapshot, trades)
            
            # Get signal from strategy
            signal = await self.strategy.on_market_data(snapshot)
            
            if signal and self._validate_signal(signal, portfolio):
                trade = await self._execute_signal(signal, portfolio, snapshot)
                trades.append(trade)
            
            # Update equity curve
            value = self._calculate_portfolio_value(portfolio, snapshot)
            portfolio.equity_history.append((snapshot.timestamp, value))
        
        # Close remaining positions
        await self._close_all_positions(portfolio, trades)
        
        # Calculate metrics
        metrics = calculate_metrics(portfolio.equity_history, trades)
        
        return BacktestResult(
            initial_capital=self.config.initial_capital,
            final_value=portfolio.cash,
            metrics=metrics,
            equity_curve=portfolio.equity_history,
            trades=trades
        )
```

---

## Running Backtests

### Via API

```bash
# Run Binary Complement Arbitrage
curl -X POST http://localhost:8000/api/v1/backtests \
  -H "Content-Type: application/json" \
  -d '{
    "strategy": "binary_complement_arbitrage",
    "strategy_config": {"min_edge": 0.02},
    "start_date": "2024-06-01T00:00:00Z",
    "end_date": "2024-12-01T00:00:00Z",
    "initial_capital": 10000
  }'

# Run Whale Copy-Trading
curl -X POST http://localhost:8000/api/v1/backtests \
  -H "Content-Type: application/json" \
  -d '{
    "strategy": "whale_copy_trading",
    "strategy_config": {
      "min_pnl": 10000,
      "min_win_rate": 0.55,
      "copy_delay_seconds": 30
    },
    "start_date": "2024-06-01T00:00:00Z",
    "end_date": "2024-12-01T00:00:00Z",
    "initial_capital": 10000
  }'
```

### Via Claude Code

```
/project:run-backtest binary_complement_arbitrage --start 2024-06-01 --end 2024-12-01 --capital 10000

/project:run-backtest catalyst_momentum --start 2024-10-01 --end 2024-11-15 --capital 50000

/project:run-backtest favorite_compounder --start 2024-01-01 --end 2024-12-01 --capital 25000
```

---

## Strategy Summary Table

| Strategy | Key Config | When to Use |
|----------|-----------|-------------|
| **Binary Complement Arb** | `min_edge: 0.02` | Always-on scanner for risk-free profits |
| **Bundle Arb** | `min_edge: 0.03` | Multi-outcome events (elections, awards) |
| **Catalyst Momentum** | `price_threshold: 5%`, `volume_spike: 3x` | Breaking news, major announcements |
| **Settlement Edge** | `min_edge: 0.10` | Complex resolution rules, legal/technical criteria |
| **Term Spreads** | `min_spread: 0.05` | Same event, different dates |
| **Correlation Hedge** | `z_score: 2.0` | Related markets (Fed, macro) |
| **Cross-Platform Arb** | `min_edge: 0.03` | Polymarket vs Kalshi pricing gaps |
| **Favorite Compounder** | `min_prob: 0.85` | Near-certain outcomes, short expiry |
| **No Bias Exploit** | `max_yes: 0.40` | "Will X say Y?" mention markets |
| **Whale Copy** | `min_pnl: $10k`, `min_wr: 55%` | Following smart money |

---

## Quick Start Checklist

1. ☐ Set up PostgreSQL + TimescaleDB
2. ☐ Run database migrations
3. ☐ Sync markets from Polymarket
4. ☐ Backfill historical price data
5. ☐ Configure strategy parameters
6. ☐ Run backtest via API or CLI
7. ☐ Analyze results and iterate
8. ☐ Deploy live with paper trading first
