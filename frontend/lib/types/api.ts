/**
 * API Type Definitions
 * ====================
 * Common types for API responses and requests
 */

// ============================================================================
// Live Quote Types
// ============================================================================

export interface LiveQuote {
  symbol: string;
  ltp: number;
  change: number;
  change_pct: number;
  volume: number;
  high: number;
  low: number;
  open: number;
  prev_close: number;
  timestamp: string;
}

// ============================================================================
// Index Types
// ============================================================================

export interface IndexInfo {
  id: string;
  name: string;
  count: number;
}

export interface IndicesResponse {
  indices: IndexInfo[];
}

// ============================================================================
// Strategy Types
// ============================================================================

export interface StrategyInfo {
  id: string;
  name: string;
  description: string;
  asset_types: string[];
}

export interface StrategiesResponse {
  strategies: StrategyInfo[];
}

// ============================================================================
// Universe Types
// ============================================================================

export interface UniverseInfo {
  id: string;
  name: string;
  symbol_count: number;
  description?: string;
}

export interface UniversesResponse {
  universes: UniverseInfo[];
}

// ============================================================================
// Backtest Config Types
// ============================================================================

export interface SavedBacktestConfig {
  id: number;
  name: string;
  asset_type: string;
  strategy: string;
  config: Record<string, unknown>;
  description?: string;
  tags?: string[];
  created_at: string;
  updated_at: string;
}

export interface SavedConfigsResponse {
  configs: SavedBacktestConfig[];
}

// ============================================================================
// Backtest Run Types
// ============================================================================

export interface BacktestRunInfo {
  run_id: string;
  name: string;
  asset_type: string;
  strategy: string;
  start_date: string;
  end_date: string;
  initial_capital: number;
  final_capital: number;
  total_return: number;
  sharpe_ratio: number;
  max_drawdown: number;
  status: 'running' | 'completed' | 'failed';
  created_at: string;
}

export interface BacktestRunsResponse {
  runs: BacktestRunInfo[];
  total: number;
}

// ============================================================================
// Backtest Comparison Types
// ============================================================================

export interface BacktestComparisonMetrics {
  total_return: number;
  sharpe_ratio: number;
  max_drawdown: number;
  win_rate: number;
  profit_factor: number;
  total_trades: number;
}

export interface BacktestComparisonRun {
  runId: string;
  assetType: string;
  strategy: string;
  metrics: BacktestComparisonMetrics;
}

export interface BacktestComparison {
  id: number;
  name: string;
  description?: string;
  runs: BacktestComparisonRun[];
  created_at: string;
}

export interface ComparisonResponse {
  id: number;
  comparison: BacktestComparison;
}
