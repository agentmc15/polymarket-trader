Run a backtest for strategy: $ARGUMENTS

## Parse Arguments

Extract from arguments:
- Strategy name (required)
- Start date (optional, default: 90 days ago)
- End date (optional, default: today)
- Initial capital (optional, default: $10,000)
- Market filter (optional, specific markets to test)

## Pre-flight Checks

1. Verify strategy exists in app/strategies/
2. Check historical data availability for date range
3. Validate configuration parameters

## Data Preparation

1. **Fetch Historical Data**:
   ```python
   # Fetch from TimescaleDB
   data = await market_data_service.get_historical(
       markets=market_filter,
       start=start_date,
       end=end_date,
       interval="1m"  # or appropriate interval
   )
   ```

2. **Prepare Market States**:
   - Convert raw data to MarketState objects
   - Include orderbook snapshots if available
   - Add any required derived features

## Execute Backtest

1. **Configure Backtester**:
   ```python
   backtester = Backtester(
       strategy=strategy_instance,
       initial_capital=initial_capital,
       fee_rate=0.01,  # 1% fee assumption
       slippage_model=SlippageModel.PERCENTAGE(0.1)
   )
   ```

2. **Run Simulation**:
   ```python
   result = await backtester.run(
       historical_data=prepared_data,
       start_date=start_date,
       end_date=end_date
   )
   ```

## Generate Report

1. **Performance Metrics**:
   - Total Return
   - Annualized Return
   - Sharpe Ratio
   - Sortino Ratio
   - Max Drawdown
   - Win Rate
   - Profit Factor
   - Average Trade Duration
   - Number of Trades

2. **Visualizations** (save to docs/backtests/):
   - Equity curve chart
   - Drawdown chart
   - Monthly returns heatmap
   - Trade distribution histogram
   - Rolling Sharpe ratio

3. **Trade Log**:
   - Entry/exit prices
   - P&L per trade
   - Hold duration
   - Market conditions at trade time

## Benchmark Comparison

1. Compare against buy-and-hold
2. Calculate alpha and beta
3. Information ratio

## Output Files

1. **Report** (`docs/backtests/{strategy}_{timestamp}.md`):
   ```markdown
   # Backtest Report: {Strategy Name}
   
   ## Configuration
   - Period: {start} to {end}
   - Initial Capital: ${initial}
   - Markets: {markets}
   
   ## Performance Summary
   | Metric | Value |
   |--------|-------|
   | Total Return | X% |
   | Sharpe Ratio | X.XX |
   ...
   
   ## Trade Analysis
   ...
   
   ## Conclusions
   ...
   ```

2. **Raw Data** (`docs/backtests/{strategy}_{timestamp}.json`):
   - Full trade list
   - Equity curve data points
   - All computed metrics

## Quality Checks

1. Verify no lookahead bias
2. Check for survivorship bias
3. Validate fee calculations
4. Confirm slippage modeling

## Summary Output

Print summary to console:
```
Backtest Complete: {Strategy Name}
Period: {start} - {end}
Return: +X.XX% (vs +X.XX% buy-hold)
Sharpe: X.XX
Max Drawdown: -X.XX%
Trades: XXX (Win Rate: XX.X%)
Report saved to: docs/backtests/{filename}.md
```
