/**
 * Backtest Constants
 * ==================
 * Presets, market regimes, and default values
 */

import {
  AssetTypeOption,
  StrategyOption,
  PresetScenario,
  MarketRegimeDefinition
} from './types';

// ============================================================================
// Asset Type Options
// ============================================================================

export const ASSET_TYPE_OPTIONS: AssetTypeOption[] = [
  {
    value: 'stock',
    label: 'Stock',
    description: 'Single or multi-stock strategies',
    icon: 'TrendingUp',
  },
  {
    value: 'option',
    label: 'Option',
    description: 'Options strategies with Greeks',
    icon: 'Layers',
  },
  {
    value: 'index',
    label: 'Index Universe',
    description: 'Index universe reconstruction',
    icon: 'Globe',
  },
];

// ============================================================================
// Strategy Options
// ============================================================================

export const STRATEGY_OPTIONS: StrategyOption[] = [
  {
    value: 'momentum',
    label: 'Momentum',
    description: 'Buy strongest performers',
    supportedAssets: ['stock', 'index'],
  },
  {
    value: 'mean_reversion',
    label: 'Mean Reversion',
    description: 'Buy oversold, sell overbought',
    supportedAssets: ['stock', 'index'],
  },
  {
    value: 'value',
    label: 'Value',
    description: 'Buy undervalued stocks',
    supportedAssets: ['stock', 'index'],
  },
  {
    value: 'breakout',
    label: 'Breakout',
    description: 'Trade price breakouts',
    supportedAssets: ['stock'],
  },
  {
    value: 'long_call',
    label: 'Long Call',
    description: 'Buy call options',
    supportedAssets: ['option'],
  },
  {
    value: 'long_put',
    label: 'Long Put',
    description: 'Buy put options',
    supportedAssets: ['option'],
  },
  {
    value: 'covered_call',
    label: 'Covered Call',
    description: 'Own stock, sell calls',
    supportedAssets: ['option'],
  },
  {
    value: 'protective_put',
    label: 'Protective Put',
    description: 'Own stock, buy puts',
    supportedAssets: ['option'],
  },
  {
    value: 'bull_call_spread',
    label: 'Bull Call Spread',
    description: 'Moderately bullish',
    supportedAssets: ['option'],
  },
  {
    value: 'bear_put_spread',
    label: 'Bear Put Spread',
    description: 'Moderately bearish',
    supportedAssets: ['option'],
  },
  {
    value: 'iron_condor',
    label: 'Iron Condor',
    description: 'Range-bound strategy',
    supportedAssets: ['option'],
  },
  {
    value: 'straddle',
    label: 'Long Straddle',
    description: 'Buy CE + PE same strike',
    supportedAssets: ['option'],
  },
  {
    value: 'strangle',
    label: 'Long Strangle',
    description: 'Buy OTM CE + PE',
    supportedAssets: ['option'],
  },
  {
    value: 'custom',
    label: 'Custom Strategy',
    description: 'Build your own',
    supportedAssets: ['stock', 'option', 'index'],
  },
];

// ============================================================================
// Market Regimes (for realistic mock data)
// ============================================================================

export const MARKET_REGIMES: MarketRegimeDefinition[] = [
  {
    id: 'covid_crash',
    name: 'COVID Crash',
    startDate: '2020-02-01',
    endDate: '2020-04-30',
    volatility: 0.45,
    trend: -0.35,
    description: 'Market crash due to COVID-19 pandemic',
  },
  {
    id: 'covid_recovery',
    name: 'COVID Recovery',
    startDate: '2020-05-01',
    endDate: '2021-03-31',
    volatility: 0.25,
    trend: 0.65,
    description: 'Strong recovery post-COVID',
  },
  {
    id: 'sideways_2022',
    name: 'Sideways 2022',
    startDate: '2022-01-01',
    endDate: '2022-12-31',
    volatility: 0.18,
    trend: 0.02,
    description: 'Choppy markets with no clear direction',
  },
  {
    id: 'bull_run_2023',
    name: 'Bull Run 2023',
    startDate: '2023-01-01',
    endDate: '2023-12-31',
    volatility: 0.12,
    trend: 0.20,
    description: 'Strong bullish trend throughout 2023',
  },
  {
    id: 'volatile_2024',
    name: 'Volatile 2024',
    startDate: '2024-01-01',
    endDate: '2024-12-31',
    volatility: 0.22,
    trend: 0.08,
    description: 'High volatility with election uncertainty',
  },
];

// ============================================================================
// Date Range Presets
// ============================================================================

export const DATE_PRESETS = [
  { label: 'YTD', value: 'ytd' },
  { label: '1 Year', value: '1y' },
  { label: '3 Years', value: '3y' },
  { label: '5 Years', value: '5y' },
  { label: 'COVID Crash', value: '2020-02-01:2020-04-30' },
  { label: 'COVID Recovery', value: '2020-05-01:2021-03-31' },
  { label: 'Bull Run 2023', value: '2023-01-01:2023-12-31' },
  { label: '2024 Volatility', value: '2024-01-01:2024-12-31' },
];

// ============================================================================
// Default Configurations
// ============================================================================

export const DEFAULT_COSTS = {
  brokerage: 0.001,  // 0.1%
  slippage: 0.0005,  // 0.05%
  stampDuty: 0.0002, // 0.02% for India
};

export const DEFAULT_CAPITAL = 1000000; // ₹10 Lakhs

// ============================================================================
// Mock Scenarios
// ============================================================================

export const MOCK_SCENARIOS: PresetScenario[] = [
  {
    id: 'nifty50-momentum-2023',
    name: 'NIFTY 50 Momentum 2023',
    description: 'Momentum strategy on NIFTY 50 constituents during the 2023 bull run',
    config: {
      id: 'mock-1',
      name: 'NIFTY 50 Momentum 2023',
      assetType: 'index',
      universe: 'NIFTY50',
      reconstruction: true,
      dateRange: { start: '2023-01-01', end: '2023-12-31' },
      initialCapital: DEFAULT_CAPITAL,
      selectionCriteria: {
        type: 'top_n',
        metric: 'momentum',
        n: 10,
        lookbackDays: 20,
      },
      rebalancing: {
        frequency: 'monthly',
        dayOfMonth: 1,
      },
      costs: DEFAULT_COSTS,
    },
  },
  {
    id: 'nifty-atm-straddle',
    name: 'NIFTY ATM Straddle (Volatility Play)',
    description: 'Short ATM straddle on NIFTY weekly options to capture volatility premium',
    config: {
      id: 'mock-2',
      name: 'NIFTY ATM Straddle',
      assetType: 'option',
      underlying: 'NIFTY',
      optionSelection: {
        type: 'both',
        strikeSelection: 'ATM',
        expirySelection: 'weekly',
        rollStrategy: 'at_expiry',
      },
      strategy: 'straddle',
      dateRange: { start: '2024-01-01', end: '2024-12-31' },
      initialCapital: 500000,
      costs: { brokerage: 0.002, slippage: 0.001 },
    },
  },
  {
    id: 'reliance-mean-reversion',
    name: 'Reliance Mean Reversion',
    description: 'RSI-based mean reversion on RELIANCE stock',
    config: {
      id: 'mock-3',
      name: 'Reliance Mean Reversion',
      assetType: 'stock',
      symbols: ['RELIANCE'],
      positionSizing: { type: 'percent_of_equity', value: 100 },
      maxPositions: 1,
      longShort: 'long',
      dateRange: { start: '2022-01-01', end: '2024-12-31' },
      initialCapital: 100000,
      costs: { brokerage: 0.0005, slippage: 0.0002 },
    },
  },
  {
    id: 'banknifty-breakout',
    name: 'BankNifty Breakout Strategy',
    description: 'Donchian Channel breakout strategy on BANKNIFTY',
    config: {
      id: 'mock-4',
      name: 'BankNifty Breakout',
      assetType: 'stock',
      symbols: ['BANKNIFTY'],
      positionSizing: { type: 'fixed', value: 1 },
      maxPositions: 1,
      longShort: 'both',
      dateRange: { start: '2023-01-01', end: '2024-12-31' },
      initialCapital: 500000,
      costs: DEFAULT_COSTS,
    },
  },
  {
    id: 'multi-stock-momentum',
    name: 'Multi-Stock Momentum Portfolio',
    description: 'Top 5 momentum stocks from NIFTY 100',
    config: {
      id: 'mock-5',
      name: 'Multi-Stock Momentum',
      assetType: 'stock',
      symbols: [], // Will be selected dynamically
      positionSizing: { type: 'percent_of_equity', value: 20 },
      maxPositions: 5,
      longShort: 'long',
      dateRange: { start: '2023-01-01', end: '2024-12-31' },
      initialCapital: DEFAULT_CAPITAL,
      costs: DEFAULT_COSTS,
    },
  },
];

// ============================================================================
// Index Universes
// ============================================================================

export const INDEX_UNIVERSES = [
  { code: 'NIFTY50', name: 'NIFTY 50', description: 'Top 50 companies by market cap' },
  { code: 'NIFTY100', name: 'NIFTY 100', description: 'Top 100 companies by market cap' },
  { code: 'NIFTY200', name: 'NIFTY 200', description: 'Top 200 companies by market cap' },
  { code: 'NIFTY500', name: 'NIFTY 500', description: 'Top 500 companies by market cap' },
  { code: 'BANKNIFTY', name: 'NIFTY Bank', description: 'Banking sector index' },
  { code: 'FINNIFTY', name: 'NIFTY Financial', description: 'Financial services index' },
  { code: 'MIDCPNIFTY', name: 'NIFTY Midcap', description: 'Midcap companies index' },
];

// ============================================================================
// Popular Symbols
// ============================================================================

export const POPULAR_SYMBOLS = [
  { symbol: 'RELIANCE', name: 'Reliance Industries', sector: 'Energy', type: 'EQUITY' },
  { symbol: 'TCS', name: 'Tata Consultancy Services', sector: 'IT', type: 'EQUITY' },
  { symbol: 'HDFCBANK', name: 'HDFC Bank', sector: 'Financial', type: 'EQUITY' },
  { symbol: 'INFY', name: 'Infosys', sector: 'IT', type: 'EQUITY' },
  { symbol: 'ICICIBANK', name: 'ICICI Bank', sector: 'Financial', type: 'EQUITY' },
  { symbol: 'SBIN', name: 'State Bank of India', sector: 'Financial', type: 'EQUITY' },
  { symbol: 'TATAMOTORS', name: 'Tata Motors', sector: 'Auto', type: 'EQUITY' },
  { symbol: 'TATASTEEL', name: 'Tata Steel', sector: 'Metals', type: 'EQUITY' },
  { symbol: 'AXISBANK', name: 'Axis Bank', sector: 'Financial', type: 'EQUITY' },
  { symbol: 'KOTAKBANK', name: 'Kotak Mahindra Bank', sector: 'Financial', type: 'EQUITY' },
  { symbol: 'ITC', name: 'ITC Limited', sector: 'FMCG', type: 'EQUITY' },
  { symbol: 'LT', name: 'Larsen & Toubro', sector: 'Infrastructure', type: 'EQUITY' },
  { symbol: 'HINDUNILVR', name: 'Hindustan Unilever', sector: 'FMCG', type: 'EQUITY' },
  { symbol: 'BHARTIARTL', name: 'Bharti Airtel', sector: 'Telecom', type: 'EQUITY' },
  { symbol: 'ASIANPAINT', name: 'Asian Paints', sector: 'Consumer', type: 'EQUITY' },
  { symbol: 'NIFTY', name: 'NIFTY 50 Index', sector: 'INDEX', type: 'INDEX' },
  { symbol: 'BANKNIFTY', name: 'NIFTY Bank Index', sector: 'INDEX', type: 'INDEX' },
  { symbol: 'FINNIFTY', name: 'NIFTY Financial', sector: 'INDEX', type: 'INDEX' },
];

// ============================================================================
// Metric Thresholds (for status indicators)
// ============================================================================

export const METRIC_THRESHOLDS = {
  sharpeRatio: { good: 1.5, warning: 1.0 },
  maxDrawdown: { good: -0.15, warning: -0.25 }, // Less negative is better
  winRate: { good: 0.55, warning: 0.45 },
  profitFactor: { good: 1.5, warning: 1.2 },
  calmarRatio: { good: 1.0, warning: 0.5 },
  totalReturn: { good: 0.15, warning: 0.05 },
};

// ============================================================================
// Chart Colors
// ============================================================================

export const CHART_COLORS = {
  equity: '#3b82f6',      // blue-500
  benchmark: '#6b7280',   // gray-500
  profit: '#22c55e',      // green-500
  loss: '#ef4444',        // red-500
  grid: '#374151',        // gray-700
  background: '#1f2937',  // gray-800
};

// ============================================================================
// Heatmap Colors
// ============================================================================

export const HEATMAP_COLORS = {
  positive: ['#14532d', '#166534', '#15803d', '#16a34a', '#22c55e', '#4ade80'],
  negative: ['#450a0a', '#7f1d1d', '#991b1b', '#b91c1c', '#dc2626', '#f87171'],
  neutral: '#374151',
};
