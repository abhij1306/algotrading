# Requirements Document

## Introduction

The SmartTrader backtesting system currently has critical issues affecting the accuracy of performance comparisons and the usability of the results interface. The benchmark calculation uses an unfair equal-weight daily rebalancing approach instead of a buy-and-hold index strategy, making it impossible to fairly compare strategy performance against the market. Additionally, the frontend UI has multiple rendering and usability issues that prevent traders from effectively analyzing backtest results.

This specification addresses both backend calculation accuracy and frontend user experience to deliver a reliable, professional backtesting system.

## Glossary

- **Backtest_Engine**: The backend service that executes strategy backtests and calculates performance metrics
- **Benchmark**: A reference performance baseline used to compare strategy returns (should be buy-and-hold index)
- **Equal_Weight_Rebalancing**: A portfolio construction method where all stocks are weighted equally and rebalanced daily (current incorrect benchmark approach)
- **Buy_And_Hold**: An investment strategy where an asset is purchased and held without trading (correct benchmark approach)
- **Look_Ahead_Bias**: A testing error where future information is used to make past decisions
- **Equity_Curve**: A time series chart showing portfolio value over time
- **Trade_Log**: A table displaying all entry and exit trades with prices and returns
- **Signal_Generation**: The process by which a strategy determines when to buy or sell
- **Sampling**: Reducing the number of data points displayed while preserving the overall shape of the data
- **Polling**: Repeatedly checking the server for updated backtest status
- **Design_System**: The canonical set of color tokens, spacing, and styling rules defined in docs/DESIGN_SYSTEM.md

## Requirements

### Requirement 1: Accurate Benchmark Calculation

**User Story:** As a trader, I want the benchmark to use actual index buy-and-hold returns, so that I can fairly compare my strategy performance against the market.

#### Acceptance Criteria

1. WHEN a backtest runs with universe mode, THE Backtest_Engine SHALL use the actual index price data for benchmark calculation
2. WHEN calculating benchmark returns, THE Backtest_Engine SHALL use a buy-and-hold approach (no rebalancing)
3. WHEN the index price dataset is unavailable, THE Backtest_Engine SHALL return a descriptive error message
4. THE Backtest_Engine SHALL load index prices from the consolidated index dataset at `data_system/01_sources/fyers_index_prices/universe_index_price_daily.parquet`
5. WHEN benchmark equity is calculated, THE Backtest_Engine SHALL use the formula: `initial_capital * (final_index_price / initial_index_price)`

### Requirement 2: Strategy Signal Validation

**User Story:** As a developer, I want validation that strategies generate reasonable signals, so that I can detect configuration errors early.

#### Acceptance Criteria

1. WHEN a strategy completes signal generation, THE Backtest_Engine SHALL count the total number of non-zero signals
2. IF all signals are zero for a strategy, THEN THE Backtest_Engine SHALL log a warning message
3. WHEN signal generation completes, THE Backtest_Engine SHALL log the signal count for each strategy
4. THE Backtest_Engine SHALL include signal statistics in debug logging output

### Requirement 3: Look-Ahead Bias Prevention

**User Story:** As a trader, I want assurance that strategies use only past information, so that backtest results are realistic and achievable.

#### Acceptance Criteria

1. WHEN generating signals on date D, THE Backtest_Engine SHALL use only data from dates before D
2. WHEN executing trades, THE Backtest_Engine SHALL use T+1 execution (signal on day D, execute on day D+1)
3. THE Backtest_Engine SHALL shift signals by 1 day before calculating returns
4. WHEN calculating strategy returns, THE Backtest_Engine SHALL ensure the shifted signal aligns with next-day returns

### Requirement 4: Responsive Equity Curve Chart

**User Story:** As a trader, I want the equity curve chart to render properly at all screen sizes, so that I can analyze performance on any device.

#### Acceptance Criteria

1. WHEN the results page loads, THE Frontend SHALL render the equity curve chart using ResponsiveContainer
2. THE Frontend SHALL set explicit width and height constraints on the chart container
3. WHEN the viewport resizes, THE Frontend SHALL adjust the chart dimensions automatically
4. THE Frontend SHALL use design system tokens for all chart colors and styling

### Requirement 5: Chart Data Sampling

**User Story:** As a trader, I want the equity curve to show a readable number of data points, so that I can see the overall performance trend clearly.

#### Acceptance Criteria

1. WHEN the equity curve has more than 200 data points, THE Frontend SHALL sample the data to a maximum of 200 points
2. THE Frontend SHALL use uniform sampling (every Nth point) to preserve the curve shape
3. WHEN sampling data, THE Frontend SHALL calculate the sample rate as `Math.max(1, Math.floor(total_points / 200))`
4. THE Frontend SHALL apply sampling to both equity and benchmark curves consistently

### Requirement 6: Contained Trade Log

**User Story:** As a trader, I want the trade log to stay within the viewport, so that I can scroll through trades without the page becoming unusable.

#### Acceptance Criteria

1. THE Frontend SHALL constrain the trade log container to the available viewport height
2. WHEN trades exceed the visible area, THE Frontend SHALL enable vertical scrolling within the trade log container only
3. THE Frontend SHALL use `overflow-y-auto` on the trade log container
4. THE Frontend SHALL use `min-h-0` and `flex-1` to properly size the scrollable container

### Requirement 7: Correct Win Rate Color Coding

**User Story:** As a trader, I want win rate colors to accurately reflect performance, so that I can quickly assess strategy quality.

#### Acceptance Criteria

1. WHEN win rate is greater than 50%, THE Frontend SHALL display the value in green (profit color)
2. WHEN win rate is less than 50%, THE Frontend SHALL display the value in red (loss color)
3. WHEN win rate equals 50%, THE Frontend SHALL display the value in neutral color (foreground)
4. THE Frontend SHALL use design system color tokens: `text-profit`, `text-loss`, `text-foreground`

### Requirement 8: Smart Polling Behavior

**User Story:** As a trader, I want polling to stop when appropriate, so that the system doesn't waste resources on failed or completed backtests.

#### Acceptance Criteria

1. WHEN a backtest status is "completed", THE Frontend SHALL stop polling immediately
2. WHEN a backtest status is "failed", THE Frontend SHALL stop polling immediately
3. WHEN polling encounters 3 consecutive errors, THE Frontend SHALL stop polling
4. THE Frontend SHALL reset the error count to zero after a successful poll
5. WHEN polling stops, THE Frontend SHALL clear the interval timer

### Requirement 9: Loading States and Error Boundaries

**User Story:** As a trader, I want clear feedback during loading and errors, so that I understand what the system is doing.

#### Acceptance Criteria

1. WHEN a backtest is running, THE Frontend SHALL display a loading indicator with the text "Backtest executing..."
2. WHEN a backtest fails, THE Frontend SHALL display the error message in a red error card
3. WHEN polling fails, THE Frontend SHALL display the error message at the top of the page
4. THE Frontend SHALL use design system tokens for all loading and error state styling

### Requirement 10: Design System Compliance

**User Story:** As a developer, I want all UI components to use design system tokens, so that the interface is consistent and maintainable.

#### Acceptance Criteria

1. THE Frontend SHALL use design system color tokens for all text, backgrounds, and borders
2. THE Frontend SHALL NOT use hardcoded color values (e.g., `#0a0a0a`, `#94a3b8`)
3. THE Frontend SHALL use CSS variables defined in the design system (e.g., `var(--color-surface)`)
4. THE Frontend SHALL reference docs/DESIGN_SYSTEM.md for all styling decisions

### Requirement 11: Backend Unit Test Coverage

**User Story:** As a developer, I want unit tests for critical calculation logic, so that I can prevent regressions.

#### Acceptance Criteria

1. THE Backtest_Engine SHALL have unit tests for benchmark calculation logic
2. THE Backtest_Engine SHALL have unit tests for signal generation validation
3. THE Backtest_Engine SHALL have unit tests for T+1 execution logic
4. THE Backtest_Engine SHALL have unit tests for returns calculation with shifted signals
