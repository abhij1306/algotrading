# Implementation Plan: Backtest Pipeline Fixes

## Overview

This implementation plan addresses critical accuracy and usability issues in the SmartTrader backtesting system. The work is organized into backend fixes (benchmark calculation, signal validation, T+1 verification) and frontend fixes (chart rendering, data sampling, polling behavior, design system compliance).

## Tasks

- [ ] 1. Fix backend benchmark calculation to use buy-and-hold index prices
  - [x] 1.1 Modify `Phase1BacktestService.run()` to load index price data for benchmark
    - Load index prices using existing `_load_universe_index_dataset()` method
    - Align index prices with portfolio returns dates using `reindex()` with forward fill
    - Store initial index price for buy-and-hold calculation
    - _Requirements: 1.1, 1.2, 1.4, 1.5_

  - [x] 1.2 Replace equal-weight benchmark calculation with buy-and-hold formula
    - Calculate benchmark returns as `(aligned_index / initial_index_price) - 1.0`
    - Calculate benchmark equity as `initial_capital * (1.0 + benchmark_ret)`
    - Keep fallback to equal-weight for symbol mode (when index_prices is None)
    - _Requirements: 1.2, 1.5_

  - [-] 1.3 Write unit tests for benchmark calculation
    - Test buy-and-hold calculation with known index prices
    - Test that benchmark equity matches formula: `initial_capital * (final_index / initial_index)`
    - Test fallback to equal-weight in symbol mode
    - _Requirements: 1.2, 1.5, 11.1_

  - [~] 1.4 Write property test for benchmark buy-and-hold calculation
    - **Property 1: Benchmark uses buy-and-hold calculation**
    - **Validates: Requirements 1.2, 1.5**
    - Generate random initial_capital and index price series
    - Verify benchmark_equity[i] = initial_capital * (index_price[i] / index_price[0])
    - Run minimum 100 iterations
    - _Requirements: 1.2, 1.5_

- [ ] 2. Add strategy signal validation and logging
  - [x] 2.1 Add signal counting and validation after signal generation
    - Count total non-zero signals using `signal.sum().sum()`
    - Log signal count for each strategy with strategy ID
    - Log warning if signal count is zero
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [~] 2.2 Write unit test for zero signal warning
    - Create strategy configuration that produces no signals
    - Verify warning is logged with strategy ID
    - _Requirements: 2.2_

- [ ] 3. Verify and test T+1 execution logic
  - [x] 3.1 Review existing `_signal_to_returns()` method for T+1 execution
    - Verify that `signal.shift(1)` is correctly implemented
    - Add inline comment explaining T+1 execution
    - _Requirements: 3.1, 3.2_

  - [~] 3.2 Write unit tests for signal shifting
    - Test that signals are shifted by 1 day before returns calculation
    - Test with known signal and returns series
    - Verify strategy returns use shifted signals
    - _Requirements: 3.2, 11.3, 11.4_

  - [~] 3.3 Write property test for T+1 execution
    - **Property 2: T+1 execution prevents look-ahead bias**
    - **Validates: Requirements 3.1, 3.2**
    - Generate random signal and returns series
    - Verify strategy_returns[i] uses signal[i-1], not signal[i]
    - Run minimum 100 iterations
    - _Requirements: 3.1, 3.2_

- [~] 4. Checkpoint - Ensure backend tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 5. Implement frontend chart data sampling
  - [x] 5.1 Add data sampling logic to chartRows useMemo
    - Calculate sample rate as `Math.max(1, Math.floor(rows.length / 200))`
    - Filter rows using modulo: `rows.filter((_, i) => i % sampleRate === 0)`
    - Apply sampling to combined equity/benchmark data
    - _Requirements: 5.1, 5.2, 5.3, 5.4_

  - [~] 5.2 Write unit tests for data sampling
    - Test sampling with 250 points (should result in ~200 points)
    - Test sampling with 500 points (should result in ~200 points)
    - Test that sample rate calculation is correct
    - _Requirements: 5.1, 5.3_

  - [~] 5.3 Write property test for chart data sampling
    - **Property 3: Chart data sampling limits points to 200**
    - **Validates: Requirements 5.1, 5.3**
    - Generate random equity curves with 201-5000 points
    - Verify sampled data has at most 200 points
    - Run minimum 100 iterations
    - _Requirements: 5.1, 5.3_

  - [~] 5.4 Write property test for sampling consistency
    - **Property 4: Sampling is consistent across curves**
    - **Validates: Requirements 5.4**
    - Generate random equity and benchmark curves with same length
    - Verify both have same length after sampling
    - Run minimum 100 iterations
    - _Requirements: 5.4_

- [~] 6. Fix chart rendering with ResponsiveContainer
  - [x] 6.1 Replace fixed-width LineChart with ResponsiveContainer
    - Wrap LineChart in ResponsiveContainer with width="100%" height="100%"
    - Remove fixed width prop from LineChart
    - Keep explicit height on CardContent container (360px)
    - _Requirements: 4.1, 4.2, 4.3_

  - [~] 6.2 Write unit test for chart responsive rendering
    - Test that chart renders at different viewport sizes
    - Verify ResponsiveContainer is used
    - _Requirements: 4.3_

- [x] 7. Replace hardcoded colors with design system tokens
  - [x] 7.1 Replace all hardcoded color values in chart components
    - Replace `#94a3b8` with `var(--color-foreground-muted)`
    - Replace `#0a0a0a` with `var(--color-background)`
    - Replace `#1f1f1f` with `var(--color-elevated)`
    - Replace `#2a2a2a` with `var(--color-border)`
    - Replace `#fafafa` with `var(--color-foreground)`
    - Replace `#60a5fa` with `var(--color-primary)`
    - _Requirements: 4.4, 10.1, 10.2, 10.3_

  - [x] 7.2 Update chart axis and tooltip styling to use design tokens
    - Update XAxis stroke and tick colors to use CSS variables
    - Update YAxis stroke and tick colors to use CSS variables
    - Update Tooltip contentStyle to use CSS variables
    - _Requirements: 4.4, 10.1, 10.3_

- [~] 8. Fix win rate color coding logic
  - [x] 8.1 Correct win rate color conditional logic
    - Change condition to: `winRate > 50 ? 'text-profit' : winRate < 50 ? 'text-loss' : 'text-foreground'`
    - Ensure design system tokens are used (text-profit, text-loss, text-foreground)
    - _Requirements: 7.1, 7.2, 7.3, 7.4_

  - [~] 8.2 Write unit tests for win rate color logic
    - Test 60% win rate shows 'text-profit'
    - Test 40% win rate shows 'text-loss'
    - Test 50% win rate shows 'text-foreground'
    - _Requirements: 7.1, 7.2, 7.3_

  - [~] 8.3 Write property test for win rate color mapping
    - **Property 5: Win rate color mapping**
    - **Validates: Requirements 7.1, 7.2**
    - Generate random win_rate values from 0 to 100
    - Verify color class matches expected based on value
    - Run minimum 100 iterations
    - _Requirements: 7.1, 7.2_

- [~] 9. Implement smart polling behavior
  - [x] 9.1 Add error counting and termination logic to polling useEffect
    - Add errorCount state variable
    - Increment errorCount on poll errors
    - Reset errorCount to 0 on successful poll
    - Stop polling after 3 consecutive errors
    - Stop polling when status is 'completed' or 'failed'
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_

  - [~] 9.2 Write unit tests for polling termination
    - Test polling stops when status is 'completed'
    - Test polling stops when status is 'failed'
    - Test polling stops after 3 consecutive errors
    - Test error count resets after successful poll
    - _Requirements: 8.1, 8.2, 8.3, 8.4_

  - [~] 9.3 Write property test for error count reset
    - **Property 6: Error count resets on success**
    - **Validates: Requirements 8.4**
    - Generate random sequences of success/error poll responses
    - Verify after any success, errorCount === 0
    - Run minimum 100 iterations
    - _Requirements: 8.4_

  - [~] 9.4 Write property test for polling cleanup
    - **Property 7: Polling cleanup on termination**
    - **Validates: Requirements 8.5**
    - Generate random termination conditions
    - Verify clearInterval called exactly once
    - Run minimum 100 iterations
    - _Requirements: 8.5_

- [x] 10. Fix trade log container scrolling
  - [x] 10.1 Update trade log container CSS classes
    - Add `min-h-0` and `flex-1` to scrollable container div
    - Add `overflow-y-auto` to enable vertical scrolling
    - Ensure parent container has `overflow-hidden`
    - _Requirements: 6.1, 6.2, 6.3, 6.4_

- [~] 11. Add loading states and error boundaries
  - [x] 11.1 Ensure loading state displays correctly
    - Verify "Backtest executing..." message shows when status is 'running'
    - Use design system tokens for styling
    - _Requirements: 9.1, 9.4_

  - [x] 11.2 Ensure error states display correctly
    - Verify failed backtest shows error in red card with loss styling
    - Verify polling errors show at top of page
    - Use design system tokens: border-loss, bg-loss-bg, text-loss
    - _Requirements: 9.2, 9.3, 9.4_

  - [~] 11.3 Write unit tests for loading and error states
    - Test loading indicator displays when status is 'running'
    - Test error card displays when status is 'failed'
    - Test polling error displays at top of page
    - _Requirements: 9.1, 9.2, 9.3_

- [~] 12. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties with minimum 100 iterations
- Unit tests validate specific examples and edge cases
- Backend uses pytest and Hypothesis for testing
- Frontend uses Jest, React Testing Library, and fast-check for testing
