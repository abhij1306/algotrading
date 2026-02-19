# Design Document: Backtest Pipeline Fixes

## Overview

This design addresses critical accuracy and usability issues in the SmartTrader backtesting system. The primary backend issue is that the benchmark calculation uses equal-weight daily rebalancing of all stocks (`returns.mean(axis=1)`) instead of a buy-and-hold index approach, making performance comparisons unfair and misleading. The frontend has multiple UI issues including chart rendering problems, uncontained scrolling, incorrect color coding, and inefficient polling behavior.

The solution involves:
1. Replacing the benchmark calculation to use actual index price data with buy-and-hold logic
2. Adding validation for strategy signal generation to catch configuration errors
3. Ensuring T+1 execution to prevent look-ahead bias
4. Fixing frontend chart rendering with proper responsive containers and data sampling
5. Implementing smart polling with error handling and proper termination
6. Ensuring full design system compliance

## Architecture

### Backend Changes

The `Phase1BacktestService` in `backend/app/services/backtest_phase1_service.py` will be modified to:
- Load index price data from the consolidated dataset at `data_system/01_sources/fyers_index_prices/universe_index_price_daily.parquet`
- Calculate benchmark returns using buy-and-hold logic: `(final_price / initial_price) - 1`
- Add signal validation logging after strategy signal generation
- Verify that signal shifting is correctly implemented for T+1 execution

### Frontend Changes

The results page at `frontend/app/backtest/results/[runId]/page.tsx` will be modified to:
- Implement proper chart data sampling (max 200 points)
- Fix chart rendering with explicit dimensions
- Constrain trade log scrolling to viewport
- Correct win rate color logic
- Implement smart polling with error counting and termination
- Replace all hardcoded colors with design system tokens

### Data Flow

```
1. Backtest Request → Phase1BacktestService.run()
2. Load universe snapshot data (existing)
3. Load index price data (NEW) → _load_universe_index_dataset()
4. Generate strategy signals → _build_signal()
5. Validate signals (NEW) → log signal counts
6. Shift signals by 1 day (verify existing)
7. Calculate strategy returns with shifted signals
8. Calculate benchmark returns (MODIFIED) → buy-and-hold from index prices
9. Return results with equity curves
10. Frontend polls for results
11. Frontend samples data (NEW) → max 200 points
12. Frontend renders charts with design tokens (MODIFIED)
```

## Components and Interfaces

### Backend: Phase1BacktestService

#### Modified Method: `_load_universe_index_dataset`

This method already exists and loads index price data. We'll use it for benchmark calculation.

```python
def _load_universe_index_dataset(self, universe: str, start: date, end: date) -> pd.DataFrame:
    """Load index-level daily closes from the consolidated local index dataset."""
    # Existing implementation - already correct
    # Returns DataFrame with columns: date, symbol, price
```

#### Modified Method: `run`

```python
def run(self, payload: dict[str, Any]) -> dict[str, Any]:
    # ... existing code for loading data ...

    # NEW: Load index price data for benchmark
    if mode == "universe":
        index_prices = self._load_universe_index_dataset(universe, start, end)
        index_prices = index_prices[(index_prices["date"] >= start) & (index_prices["date"] <= end)].copy()
        index_prices = index_prices.set_index("date")["price"]
        index_prices.index = pd.to_datetime(index_prices.index)
    else:
        # For symbol mode, use equal-weight as fallback
        index_prices = None

    # ... existing signal generation code ...

    # NEW: Validate signals after generation
    for strategy_cfg in strategy_allocations:
        signal = self._build_signal(prices, strategy_cfg["strategy_id"], strategy_cfg["params"])
        signal_count = int(signal.sum().sum())
        logger.info(f"Strategy {strategy_cfg['strategy_id']}: generated {signal_count} total signals")
        if signal_count == 0:
            logger.warning(f"Strategy {strategy_cfg['strategy_id']}: NO SIGNALS GENERATED - check parameters")

    # ... existing returns calculation ...

    # MODIFIED: Calculate benchmark returns using buy-and-hold
    if index_prices is not None:
        # Buy-and-hold: (final / initial) - 1
        aligned_index = index_prices.reindex(portfolio_ret.index, method='ffill')
        initial_index_price = aligned_index.iloc[0]
        benchmark_ret = (aligned_index / initial_index_price) - 1.0
        benchmark_equity = initial_capital * (1.0 + benchmark_ret)
    else:
        # Fallback for symbol mode
        benchmark_ret = returns.mean(axis=1).fillna(0.0)
        benchmark_equity = initial_capital * (1.0 + benchmark_ret).cumprod()

    # ... rest of existing code ...
```

#### Verification: Signal Shifting for T+1 Execution

The existing `_signal_to_returns` method already implements T+1 execution correctly:

```python
def _signal_to_returns(self, signal: pd.DataFrame, returns: pd.DataFrame) -> pd.Series:
    shifted = signal.shift(1).fillna(0.0)  # ← This implements T+1
    denom = shifted.sum(axis=1).replace(0.0, np.nan)
    weights = shifted.div(denom, axis=0).fillna(0.0)
    series = (weights * returns).sum(axis=1).fillna(0.0)
    series.index = pd.to_datetime(series.index)
    return series
```

The `shift(1)` operation ensures that signals generated on day D are used to calculate returns on day D+1, preventing look-ahead bias.

### Frontend: BacktestResultPage

#### Modified Hook: useEffect for Polling

```typescript
useEffect(() => {
  let active = true;
  let intervalId: NodeJS.Timeout | null = null;
  let errorCount = 0;

  const poll = async () => {
    const res = await apiClient.get<BacktestJobResponse>(`/api/backtest/result/${runId}`);
    if (!active) return;

    if (res.error) {
      errorCount++;
      setError(res.error.message);
      // Stop polling after 3 consecutive errors
      if (errorCount >= 3 && intervalId) {
        clearInterval(intervalId);
      }
      return;
    }

    // Reset error count on success
    errorCount = 0;
    const data = res.data ?? null;
    setPayload(data);

    // Stop polling if backtest is completed or failed
    if (data && (data.status === 'completed' || data.status === 'failed')) {
      if (intervalId) clearInterval(intervalId);
    }
  };

  void poll();
  intervalId = setInterval(() => void poll(), 1200);

  return () => {
    active = false;
    if (intervalId) clearInterval(intervalId);
  };
}, [runId]);
```

#### Modified Computation: Chart Data Sampling

```typescript
const chartRows = useMemo(() => {
  const eq = payload?.result?.equity_curve ?? [];
  const benchmark = payload?.result?.benchmark_curve ?? [];
  const benchMap = new Map(benchmark.map((b) => [b.date, b.equity]));
  const rows = eq.map((e) => ({
    date: e.date,
    equity: e.equity,
    benchmark: benchMap.get(e.date) ?? null
  }));

  // Sample data to show max 200 points for cleaner chart
  const sampleRate = Math.max(1, Math.floor(rows.length / 200));
  const sampled = rows.filter((_, i) => i % sampleRate === 0);

  return sampled;
}, [payload]);
```

#### Modified Component: Win Rate Display

```typescript
const winRate = metrics?.win_rate_pct ?? 0;

// In the metrics grid:
<div className={`mono-num text-2xl font-semibold ${
  winRate > 50 ? 'text-profit' :
  winRate < 50 ? 'text-loss' :
  'text-foreground'
}`}>
  {metrics ? `${winRate.toFixed(1)}%` : '--'}
</div>
```

#### Modified Component: Chart Container

```typescript
<CardContent className="p-4" style={{ height: '360px' }}>
  {chartRows.length === 0 ? (
    <div className="flex h-full items-center justify-center rounded-md border border-border bg-background-secondary text-sm text-foreground-muted">
      No equity curve data available for this run.
    </div>
  ) : (
    <ResponsiveContainer width="100%" height="100%">
      <LineChart
        data={chartRows}
        margin={{ top: 10, right: 30, left: 60, bottom: 40 }}
      >
        <XAxis
          dataKey="date"
          height={60}
          angle={-45}
          textAnchor="end"
          stroke="var(--color-foreground-muted)"
          tick={{ fill: 'var(--color-foreground-muted)', fontSize: 10 }}
          axisLine={{ stroke: 'var(--color-border)' }}
          tickLine={{ stroke: 'var(--color-border)' }}
        />
        <YAxis
          width={60}
          stroke="var(--color-foreground-muted)"
          tick={{ fill: 'var(--color-foreground-secondary)', fontSize: 11 }}
          axisLine={{ stroke: 'var(--color-border)' }}
          tickLine={{ stroke: 'var(--color-border)' }}
        />
        <Tooltip
          contentStyle={{
            backgroundColor: 'var(--color-elevated)',
            border: '1px solid var(--color-border)',
            borderRadius: 'var(--radius-md)',
            color: 'var(--color-foreground)',
            fontSize: '12px'
          }}
        />
        <Line
          type="monotone"
          dataKey="equity"
          stroke="var(--color-primary)"
          strokeWidth={2.5}
          dot={false}
          name="Portfolio"
        />
        <Line
          type="monotone"
          dataKey="benchmark"
          stroke="var(--color-foreground-muted)"
          strokeWidth={1.8}
          strokeDasharray="5 5"
          dot={false}
          name="Benchmark"
        />
      </LineChart>
    </ResponsiveContainer>
  )}
</CardContent>
```

#### Modified Component: Trade Log Container

```typescript
<div className="flex h-full min-h-0 flex-col overflow-hidden bg-background-secondary">
  <Card variant="void" className="shrink-0 rounded-none border-0 border-b border-border bg-surface">
    <CardHeader className="px-4 py-3">
      <div className="flex items-center justify-between">
        <CardTitle className="text-foreground">Trade Log</CardTitle>
        <Badge variant="outline">{trades.length} trades</Badge>
      </div>
      <p className="text-xs text-foreground-muted">{winRate.toFixed(1)}% win rate · by date</p>
    </CardHeader>
  </Card>

  <div className="min-h-0 flex-1 overflow-y-auto custom-scrollbar">
    {trades.length === 0 ? (
      <div className="p-4 text-sm text-foreground-secondary">
        No trades generated for this configuration.
      </div>
    ) : (
      <Table>
        {/* ... table content ... */}
      </Table>
    )}
  </div>
</div>
```

## Data Models

No changes to existing data models are required. The backtest response schema remains the same:

```typescript
type BacktestJobResponse = {
  job_id: string;
  status: 'running' | 'completed' | 'failed';
  created_at: string;
  error?: string;
  params: {
    instrument_type: string;
    selection: { mode: string; universe?: string; symbols?: string[] };
    start_date: string;
    end_date: string;
  };
  result?: {
    selection: { scope: string };
    metrics: {
      final_equity: number;
      total_return_pct: number;
      sharpe_ratio: number;
      max_drawdown_pct: number;
      total_trades: number;
      win_rate_pct: number;
    };
    equity_curve: Array<{ date: string; equity: number }>;
    benchmark_curve: Array<{ date: string; equity: number }>;
    drawdown_curve: Array<{ date: string; drawdown_pct: number }>;
    trade_log: Array<{
      symbol: string;
      entry_date: string;
      exit_date: string;
      entry_price: number;
      exit_price: number;
      return_pct: number;
    }>;
  };
};
```


## Correctness Properties

A property is a characteristic or behavior that should hold true across all valid executions of a system—essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.

### Backend Properties

Property 1: Benchmark uses buy-and-hold calculation
*For any* backtest run in universe mode with valid index price data, the benchmark equity at any point should equal `initial_capital * (index_price_at_time / initial_index_price)`
**Validates: Requirements 1.2, 1.5**

Property 2: T+1 execution prevents look-ahead bias
*For any* strategy and any date D, the signal generated on date D should only affect returns calculated on date D+1 or later (signals must be shifted by 1 day before applying to returns)
**Validates: Requirements 3.1, 3.2**

### Frontend Properties

Property 3: Chart data sampling limits points to 200
*For any* equity curve with more than 200 data points, the sampled data should contain at most 200 points, calculated using sample rate `Math.max(1, Math.floor(total_points / 200))`
**Validates: Requirements 5.1, 5.3**

Property 4: Sampling is consistent across curves
*For any* backtest result, the equity curve and benchmark curve should have the same number of points after sampling
**Validates: Requirements 5.4**

Property 5: Win rate color mapping
*For any* win rate value, the color class should be 'text-profit' when win_rate > 50, 'text-loss' when win_rate < 50, and 'text-foreground' when win_rate = 50
**Validates: Requirements 7.1, 7.2**

Property 6: Error count resets on success
*For any* polling sequence, after a successful poll response, the error count should be reset to zero
**Validates: Requirements 8.4**

Property 7: Polling cleanup on termination
*For any* polling session that terminates (due to completion, failure, or errors), the interval timer should be cleared
**Validates: Requirements 8.5**

## Error Handling

### Backend Error Handling

1. **Missing Index Dataset**: When the index price dataset file is not found, raise `FileNotFoundError` with message: "Index dataset missing: {path}. Download and consolidate index prices first."

2. **No Index Data for Universe**: When the index dataset exists but contains no data for the requested universe and date range, raise `ValueError` with message: "No index dataset rows for universe={universe_id} in range {start}..{end}"

3. **Signal Generation Warnings**: When a strategy generates zero signals, log a warning but continue execution (this is not a fatal error, just a configuration issue)

### Frontend Error Handling

1. **Polling Errors**: Display error messages at the top of the page in a red error banner. Stop polling after 3 consecutive errors to prevent infinite retry loops.

2. **Failed Backtests**: Display the error message from the backend in a red card with loss styling (`border-loss`, `bg-loss-bg`, `text-loss`).

3. **Missing Data**: When equity curve or trade log data is empty, display a friendly message in a bordered container rather than showing an empty chart or table.

## Testing Strategy

### Dual Testing Approach

This feature requires both unit tests and property-based tests for comprehensive coverage:

- **Unit tests**: Verify specific examples, edge cases, and error conditions
- **Property tests**: Verify universal properties across all inputs

### Backend Testing

#### Unit Tests

1. **Benchmark Calculation Example**: Test that a backtest with known index prices produces the expected benchmark equity
   - Given: initial_capital=1000000, initial_index=100, final_index=110
   - Expected: final_benchmark_equity = 1100000

2. **Missing Index Dataset**: Test that missing index file raises appropriate error
   - Given: index dataset file does not exist
   - Expected: FileNotFoundError with descriptive message

3. **Zero Signals Warning**: Test that strategies generating zero signals log a warning
   - Given: strategy parameters that produce no signals
   - Expected: Warning logged with strategy ID

4. **Signal Shifting**: Test that signals are shifted by 1 day before returns calculation
   - Given: signal=[0,1,1,0], returns=[0.01,0.02,0.03,0.04]
   - Expected: strategy_returns uses shifted signal [NaN,0,1,1] → [0, 0, 0.03, 0.04]

#### Property Tests

Configure each property test to run minimum 100 iterations.

1. **Property 1: Benchmark buy-and-hold calculation**
   - **Feature: backtest-pipeline-fixes, Property 1**: For any backtest run in universe mode with valid index price data, the benchmark equity at any point should equal initial_capital * (index_price_at_time / initial_index_price)
   - Generate: random initial_capital, random index price series
   - Verify: benchmark_equity[i] = initial_capital * (index_price[i] / index_price[0])

2. **Property 2: T+1 execution**
   - **Feature: backtest-pipeline-fixes, Property 2**: For any strategy and any date D, the signal generated on date D should only affect returns calculated on date D+1 or later
   - Generate: random signal series, random returns series
   - Verify: strategy_returns[i] uses signal[i-1], not signal[i]

### Frontend Testing

#### Unit Tests

1. **Sampling with 250 points**: Test that 250 points are sampled to 200
   - Given: equity_curve with 250 points
   - Expected: sampled data has 200 points (sample_rate = 1, every point taken)

2. **Sampling with 500 points**: Test that 500 points are sampled to 200
   - Given: equity_curve with 500 points
   - Expected: sampled data has 200 points (sample_rate = 2, every 2nd point taken)

3. **Win rate color at 60%**: Test that 60% win rate shows green
   - Given: win_rate_pct = 60
   - Expected: className includes 'text-profit'

4. **Win rate color at 40%**: Test that 40% win rate shows red
   - Given: win_rate_pct = 40
   - Expected: className includes 'text-loss'

5. **Win rate color at 50%**: Test that 50% win rate shows neutral
   - Given: win_rate_pct = 50
   - Expected: className includes 'text-foreground'

6. **Polling stops on completed**: Test that polling stops when status is completed
   - Given: backtest status changes to 'completed'
   - Expected: clearInterval called, no more poll requests

7. **Polling stops after 3 errors**: Test that polling stops after 3 consecutive errors
   - Given: 3 consecutive failed poll requests
   - Expected: clearInterval called

#### Property Tests

Configure each property test to run minimum 100 iterations.

1. **Property 3: Chart data sampling**
   - **Feature: backtest-pipeline-fixes, Property 3**: For any equity curve with more than 200 data points, the sampled data should contain at most 200 points
   - Generate: random equity curves with varying lengths (201 to 5000 points)
   - Verify: sampled.length <= 200 AND sampled.length = Math.ceil(original.length / Math.max(1, Math.floor(original.length / 200)))

2. **Property 4: Sampling consistency**
   - **Feature: backtest-pipeline-fixes, Property 4**: For any backtest result, the equity curve and benchmark curve should have the same number of points after sampling
   - Generate: random equity and benchmark curves with same length
   - Verify: sampled_equity.length === sampled_benchmark.length

3. **Property 5: Win rate color mapping**
   - **Feature: backtest-pipeline-fixes, Property 5**: For any win rate value, the color class should be correct
   - Generate: random win_rate values from 0 to 100
   - Verify: color class matches expected based on value (>50 → profit, <50 → loss, =50 → foreground)

4. **Property 6: Error count reset**
   - **Feature: backtest-pipeline-fixes, Property 6**: For any polling sequence, after a successful poll response, the error count should be reset to zero
   - Generate: random sequences of success/error poll responses
   - Verify: after any success, errorCount === 0

5. **Property 7: Polling cleanup**
   - **Feature: backtest-pipeline-fixes, Property 7**: For any polling session that terminates, the interval timer should be cleared
   - Generate: random termination conditions (completed, failed, 3 errors)
   - Verify: clearInterval called exactly once

### Testing Libraries

- **Backend**: pytest for unit tests, Hypothesis for property-based tests
- **Frontend**: Jest + React Testing Library for unit tests, fast-check for property-based tests

### Test Organization

```
backend/tests/
  test_backtest_benchmark.py          # Unit tests for benchmark calculation
  test_backtest_signal_validation.py  # Unit tests for signal validation
  test_backtest_t1_execution.py       # Unit tests for T+1 execution
  property_test_benchmark.py          # Property tests for benchmark
  property_test_signal_shifting.py    # Property tests for T+1 execution

frontend/__tests__/
  backtest-results-sampling.test.tsx  # Unit tests for data sampling
  backtest-results-colors.test.tsx    # Unit tests for color logic
  backtest-results-polling.test.tsx   # Unit tests for polling behavior
  property-test-sampling.test.tsx     # Property tests for sampling
  property-test-colors.test.tsx       # Property tests for color mapping
  property-test-polling.test.tsx      # Property tests for polling logic
```
