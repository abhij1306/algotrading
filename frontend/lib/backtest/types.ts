/**
 * Backtest Type Definitions
 * =========================
 * TypeScript interfaces for the backtesting system
 */

// ============================================================================
// Base Types
// ============================================================================

export type AssetType = 'stock' | 'option' | 'index';

export type RebalanceFrequency = 'daily' | 'weekly' | 'monthly';

export type OptionType = 'CE' | 'PE' | 'both';

export type StrikeSelection = 'ATM' | 'ITM' | 'OTM' | 'percent_otm';

export type ExpirySelection = 'weekly' | 'monthly' | 'days_to_expiry';

export type RollStrategy = 'none' | 'at_expiry' | 'days_before' | 'delta_based';

export type StrategyType =
  | 'momentum'
  | 'mean_reversion'
  | 'value'
  | 'breakout'
  | 'long_call'
  | 'long_put'
  | 'covered_call'
  | 'protective_put'
  | 'bull_call_spread'
  | 'bear_put_spread'
  | 'iron_condor'
  | 'straddle'
  | 'strangle'
  | 'custom';

// ============================================================================
// Configuration Types
// ============================================================================

export interface BaseBacktestConfig {
  id: string;
  name: string;
  assetType: AssetType;
  dateRange: {
    start: string;  // YYYY-MM-DD
    end: string;    // YYYY-MM-DD
  };
  initialCapital: number;
  costs: {
    brokerage: number;  // % per trade
    slippage: number;   // % per trade
    stampDuty?: number; // For Indian markets
  };
}

export interface StockBacktestConfig extends BaseBacktestConfig {
  assetType: 'stock';
  symbols: string[];
  positionSizing: {
    type: 'fixed' | 'percent_of_equity' | 'risk_based';
    value: number;
  };
  maxPositions: number;
  longShort: 'long' | 'short' | 'both';
}

export interface OptionsBacktestConfig extends BaseBacktestConfig {
  assetType: 'option';
  underlying: string;
  optionSelection: {
    type: OptionType;
    strikeSelection: StrikeSelection;
    strikeValue?: number;
    expirySelection: ExpirySelection;
    expiryValue?: number;
    rollStrategy: RollStrategy;
    rollDaysBefore?: number;
  };
  strategy: StrategyType;
  entryConditions?: {
    ivRankMin?: number;
    ivRankMax?: number;
    technicalIndicator?: string;
  };
  exitConditions?: {
    profitTarget?: number;
    stopLoss?: number;
    daysToExpiry?: number;
    deltaBased?: boolean;
  };
}

export interface IndexBacktestConfig extends BaseBacktestConfig {
  assetType: 'index';
  universe: string;
  reconstruction: boolean;
  selectionCriteria: {
    type: 'top_n' | 'percentile' | 'zscore';
    metric: 'momentum' | 'mean_reversion' | 'value' | 'custom';
    n?: number;
    lookbackDays: number;
  };
  rebalancing: {
    frequency: RebalanceFrequency;
    dayOfWeek?: number;
    dayOfMonth?: number;
  };
}

export type BacktestConfig =
  | StockBacktestConfig
  | OptionsBacktestConfig
  | IndexBacktestConfig;

// ============================================================================
// Result Types
// ============================================================================

export interface Trade {
  id: string;
  date: string;
  symbol: string;
  type: 'entry' | 'exit';
  action: 'BUY' | 'SELL';
  quantity: number;
  price: number;
  value: number;
  pnl?: number;
  return?: number;
  duration?: number; // days
  entryDate?: string;
  exitDate?: string;
}

export interface EquityPoint {
  date: string;
  equity: number;
  cash: number;
  positionsValue: number;
  drawdown: number;
}

export interface BacktestMetrics {
  // Returns
  totalReturn: number;
  cagr: number;
  annualizedReturn?: number;  // Alias for cagr
  annualizedVolatility: number;
  volatility?: number;  // Alias for annualizedVolatility

  // Risk
  sharpeRatio: number;
  sortinoRatio: number;
  maxDrawdown: number;
  maxDrawdownDuration: number;
  calmarRatio: number;
  var95: number;

  // Trades
  totalTrades: number;
  winningTrades: number;
  losingTrades: number;
  winRate: number;
  profitFactor: number;
  avgTradeReturn: number;
  avgWin: number;
  avgLoss: number;
  largestWin: number;
  largestLoss: number;
  avgTradeDuration: number;
  maxConsecutiveWins: number;
  maxConsecutiveLosses: number;
}

export interface TradeStats {
  totalTrades: number;
  winningTrades: number;
  losingTrades: number;
  averageWin: number;
  averageLoss: number;
  largestWin: number;
  largestLoss: number;
  averageTrade: number;
  averageHoldTime: number;
  maxHoldTime: number;
  minHoldTime: number;
  consecutiveWins: number;
  consecutiveLosses: number;
}

export interface MonthlyReturn {
  year: number;
  month: number;
  return: number;
}

export interface BacktestResult {
  runId: string;
  config: BacktestConfig;
  equityCurve: EquityPoint[];
  trades: Trade[];
  metrics: BacktestMetrics;
  monthlyReturns: MonthlyReturn[];
  monteCarlo?: MonteCarloResult;
  walkForward?: WalkForwardResult;
  stats?: TradeStats;
  benchmarkComparison?: {
    symbol: string;
    totalReturn: number;
    equityCurve: { date: string; value: number }[];
  };
  createdAt: string;
  status: 'running' | 'completed' | 'failed';
}

// ============================================================================
// Monte Carlo Types
// ============================================================================

export interface MonteCarloResult {
  simulations: number;
  probabilityOfProfit: number;
  probabilityOfRuin: number;
  medianFinalEquity: number;
  worstCase: number;  // 5th percentile
  bestCase: number;   // 95th percentile
  confidenceInterval: {
    lower: number;
    upper: number;
  };
  equityCurves: {
    median: number[];
    p5: number[];
    p95: number[];
  };
}

// ============================================================================
// Walk Forward Types
// ============================================================================

export interface WalkForwardWindow {
  window: number;
  inSampleStart: string;
  inSampleEnd: string;
  outOfSampleStart: string;
  outOfSampleEnd: string;
  inSampleSharpe: number;
  outOfSampleSharpe: number;
  degradation: number;
}

export interface WalkForwardResult {
  windows: WalkForwardWindow[];
  avgInSampleSharpe: number;
  avgOutOfSampleSharpe: number;
  avgDegradation: number;
  isRobust: boolean;
}

// ============================================================================
// Market Regime Types
// ============================================================================

export type MarketRegime = 'bull' | 'bear' | 'sideways';

export interface RegimePerformance {
  regime: MarketRegime;
  timeInRegime: number;  // percentage
  totalReturn: number;
  sharpeRatio: number;
  maxDrawdown: number;
  tradesCount: number;
}

// ============================================================================
// UI Types
// ============================================================================

export interface PresetScenario {
  id: string;
  name: string;
  description: string;
  config: BacktestConfig;
}

export interface AssetTypeOption {
  value: AssetType;
  label: string;
  description: string;
  icon: string;
}

export interface StrategyOption {
  value: StrategyType;
  label: string;
  description: string;
  supportedAssets: AssetType[];
}

export type MetricStatus = 'good' | 'warning' | 'danger' | 'neutral';

export interface MetricCardData {
  title: string;
  value: number | string;
  unit?: string;
  change?: number;
  status: MetricStatus;
  tooltip?: string;
  format?: 'percent' | 'number' | 'currency' | 'ratio';
}

// ============================================================================
// API Types
// ============================================================================

export interface BacktestRunRequest {
  config: BacktestConfig;
  useMockData?: boolean;
}

export interface BacktestRunResponse {
  success: boolean;
  runId?: string;
  error?: string;
  estimatedTime?: number;
}

export interface BacktestListItem {
  runId: string;
  name?: string;
  assetType: AssetType;
  strategy: string;
  dateRange: { start: string; end: string };
  initialCapital: number;
  finalCapital?: number;
  totalReturn?: number;
  sharpeRatio?: number;
  maxDrawdown?: number;
  status: 'running' | 'completed' | 'failed';
  createdAt: string;
}

export interface BacktestComparisonItem {
  id: number;
  name: string;
  description?: string;
  runs: {
    runId: string;
    assetType: AssetType;
    strategy: string;
    metrics: BacktestMetrics;
  }[];
  comparison: {
    bestReturn: number;
    bestSharpe: number;
    lowestDrawdown: number;
  };
  createdAt: string;
}

// ============================================================================
// Mock Data Types
// ============================================================================

export interface MarketRegimeDefinition {
  id: string;
  name: string;
  startDate: string;
  endDate: string;
  volatility: number;
  trend: number;
  description: string;
}

export interface MockPriceData {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface MockOptionData {
  date: string;
  strike: number;
  expiry: string;
  type: 'CE' | 'PE';
  iv: number;
  delta: number;
  gamma: number;
  theta: number;
  vega: number;
  ltp: number;
  volume: number;
  oi: number;
}
