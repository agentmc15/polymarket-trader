Create a new trading strategy: $ARGUMENTS

## Strategy Design

1. **Analyze Requirements**:
   - Parse strategy name and type from arguments
   - Identify required data sources
   - Define entry and exit signals
   - Determine risk parameters

2. **Strategy Classification**:
   - Arbitrage: Cross-market price discrepancies
   - Momentum: Trend-following signals
   - Mean Reversion: Counter-trend signals
   - Copy Trading: Following other traders
   - Event-Based: News/event-driven signals
   - Statistical: Quantitative signals

## Implementation

1. **Create Strategy File**:
   ```
   backend/app/strategies/{strategy_name}.py
   ```

2. **Implement Strategy Class**:
   ```python
   from app.strategies.base import BaseStrategy, Signal, MarketState
   
   class {StrategyName}Strategy(BaseStrategy):
       """
       {Strategy description}
       
       Parameters:
           - param1: Description
           - param2: Description
       
       Signals:
           - BUY when: condition
           - SELL when: condition
       """
       
       def __init__(self, config: dict):
           super().__init__(config)
           # Initialize strategy-specific attributes
       
       async def analyze(self, market: MarketState) -> Optional[Signal]:
           # Implement signal generation logic
           pass
       
       def calculate_position_size(
           self, signal: Signal, portfolio_value: float
       ) -> float:
           # Implement position sizing
           pass
   ```

3. **Configuration Schema**:
   - Define Pydantic model for strategy config
   - Include sensible defaults
   - Document all parameters

## Testing

1. **Unit Tests** (`tests/strategies/test_{strategy_name}.py`):
   - Test signal generation logic
   - Test position sizing
   - Test edge cases
   - Test with historical data samples

2. **Backtest Validation**:
   - Run backtest on historical data
   - Verify reasonable performance metrics
   - Check for overfitting indicators

## Documentation

1. **Strategy Doc** (`docs/strategies/{strategy_name}.md`):
   - Strategy overview
   - Mathematical basis (if applicable)
   - Configuration options
   - Expected performance characteristics
   - Risk factors
   - Example usage

## Integration

1. **Register Strategy**:
   - Add to strategy registry in app/strategies/__init__.py
   - Make available via API endpoint

2. **API Endpoints**:
   - Add endpoints for strategy configuration
   - Add endpoints for strategy backtesting
   - Add endpoints for live execution (if applicable)

## Quality Checks

1. Run tests: `pytest tests/strategies/test_{strategy_name}.py -v`
2. Type check: `mypy app/strategies/{strategy_name}.py`
3. Run sample backtest to verify

## Commit

```
feat(strategies): Add {strategy_name} strategy

- Description of strategy logic
- Configuration options
- Backtest results summary
```
