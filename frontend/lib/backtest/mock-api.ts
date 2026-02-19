/**
 * Mock Backtest API
 * =================
 * Generates realistic backtest results for development
 * Uses actual market patterns from 2020-2024
 */

import {
  BacktestConfig,
  BacktestResult,
  Trade,
  EquityPoint,
  BacktestMetrics,
  MonthlyReturn,
  MonteCarloResult,
  WalkForwardResult,
  WalkForwardWindow,
  TradeStats,
  MockPriceData,
} from './types';
import { MARKET_REGIMES, MOCK_SCENARIOS } from './constants';

// ============================================================================
// Utility Functions
// ============================================================================

function generateId(): string {
  return `BT-${Math.random().toString(36).substring(2, 10).toUpperCase()}`;
}

function addDays(date: string, days: number): string {
  const d = new Date(date);
  d.setDate(d.getDate() + days);
  return d.toISOString().split('T')[0];
}

function calculateDays(start: string, end: string): number {
  const startDate = new Date(start);
  const endDate = new Date(end);
  return Math.floor((endDate.getTime() - startDate.getTime()) / (1000 * 60 * 60 * 24));
}

function isWeekend(date: string): boolean {
  const day = new Date(date).getDay();
  return day === 0 || day === 6;
}

function randomBetween(min: number, max: number): number {
  return Math.random() * (max - min) + min;
}

function gaussianRandom(mean: number, stdDev: number): number {
  const u1 = Math.random();
  const u2 = Math.random();
  const z0 = Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2);
  return z0 * stdDev + mean;
}

// ============================================================================
// Market Regime Detection
// ============================================================================

function getMarketRegimeForDate(date: string): { volatility: number; trend: number } {
  for (const regime of MARKET_REGIMES) {
    if (date >= regime.startDate && date <= regime.endDate) {
      return { volatility: regime.volatility, trend: regime.trend };
    }
  }
  // Default regime (normal market)
  return { volatility: 0.15, trend: 0.10 };
}

// ============================================================================
// Price Data Generation
// ============================================================================

function generatePriceData(
  startDate: string,
  endDate: string,
  startPrice: number = 100
): MockPriceData[] {
  const data: MockPriceData[] = [];
  let currentPrice = startPrice;
  let currentDate = startDate;

  while (currentDate <= endDate) {
    if (!isWeekend(currentDate)) {
      const regime = getMarketRegimeForDate(currentDate);

      // Daily return based on regime
      const dailyReturn = gaussianRandom(
        regime.trend / 252, // Annual trend to daily
        regime.volatility / Math.sqrt(252) // Annual vol to daily
      );

      const open = currentPrice;
      const close = currentPrice * (1 + dailyReturn);
      const high = Math.max(open, close) * (1 + Math.random() * 0.01);
      const low = Math.min(open, close) * (1 - Math.random() * 0.01);
      const volume = Math.floor(randomBetween(1000000, 10000000));

      data.push({
        date: currentDate,
        open,
        high,
        low,
        close,
        volume,
      });

      currentPrice = close;
    }

    currentDate = addDays(currentDate, 1);
  }

  return data;
}

// ============================================================================
// Trade Generation
// ============================================================================

function generateTrades(
  config: BacktestConfig,
  priceData: MockPriceData[],
  initialCapital: number
): Trade[] {
  const trades: Trade[] = [];
  let isInPosition = false;
  let entryTrade: Partial<Trade> = {};

  const winRate = 0.58; // 58% win rate (realistic for good strategies)
  const avgHoldingDays = config.assetType === 'option' ? 5 : 15;

  for (let i = 20; i < priceData.length; i++) {
    const price = priceData[i];

    // Entry logic (simplified)
    if (!isInPosition && Math.random() < 0.05) { // ~5% chance of entry per day
      const isWin = Math.random() < winRate;
      const quantity = Math.floor(initialCapital / price.close / 10);

      entryTrade = {
        id: `T-${trades.length + 1}`,
        date: price.date,
        symbol: config.assetType === 'option'
          ? `${config.underlying || 'NIFTY'}${price.date.substring(8, 10)}CE`
          : config.assetType === 'stock' ? config.symbols?.[0] || 'NIFTY' : 'NIFTY',
        type: 'entry',
        action: 'BUY',
        quantity,
        price: price.close,
        value: quantity * price.close,
      };

      isInPosition = true;

      // Generate exit at random future date
      const exitOffset = Math.max(1, Math.floor(gaussianRandom(avgHoldingDays, 3)));
      const exitIndex = Math.min(i + exitOffset, priceData.length - 1);

      if (exitIndex > i) {
        const exitPrice = priceData[exitIndex];

        // Calculate return (winners have higher returns)
        let tradeReturn: number;
        if (isWin) {
          tradeReturn = randomBetween(0.02, 0.15); // 2-15% winners
        } else {
          tradeReturn = randomBetween(-0.08, -0.01); // 1-8% losers
        }

        const exitValue = entryTrade.value! * (1 + tradeReturn);
        const pnl = exitValue - entryTrade.value!;

        // Entry trade
        trades.push({
          ...(entryTrade as Trade),
          entryDate: price.date,
          exitDate: exitPrice.date,
          return: tradeReturn,
          pnl,
        });

        // Exit trade
        trades.push({
          id: `T-${trades.length + 1}`,
          date: exitPrice.date,
          symbol: entryTrade.symbol!,
          type: 'exit',
          action: 'SELL',
          quantity: entryTrade.quantity!,
          price: exitPrice.close,
          value: exitValue,
          pnl,
          return: tradeReturn,
          entryDate: price.date,
          exitDate: exitPrice.date,
          duration: exitOffset,
        });

        isInPosition = false;
        entryTrade = {};
        i = exitIndex; // Skip to exit date
      }
    }
  }

  return trades;
}

// ============================================================================
// Equity Curve Generation
// ============================================================================

function generateEquityCurve(
  config: BacktestConfig,
  priceData: MockPriceData[],
  trades: Trade[]
): EquityPoint[] {
  const equityCurve: EquityPoint[] = [];
  let equity = config.initialCapital;
  let cash = config.initialCapital;
  let positionsValue = 0;
  let maxEquity = equity;

  const tradeMap = new Map<string, Trade[]>();
  trades.forEach(trade => {
    if (!tradeMap.has(trade.date)) {
      tradeMap.set(trade.date, []);
    }
    tradeMap.get(trade.date)!.push(trade);
  });

  for (const price of priceData) {
    const dailyTrades = tradeMap.get(price.date) || [];

    for (const trade of dailyTrades) {
      if (trade.type === 'entry') {
        cash -= trade.value;
        positionsValue += trade.value;
      } else {
        cash += trade.value;
        positionsValue -= (trade.value - (trade.pnl || 0));
      }
    }

    equity = cash + positionsValue;
    maxEquity = Math.max(maxEquity, equity);
    const drawdown = (equity - maxEquity) / maxEquity;

    equityCurve.push({
      date: price.date,
      equity,
      cash,
      positionsValue,
      drawdown,
    });
  }

  return equityCurve;
}

// ============================================================================
// Metrics Calculation
// ============================================================================

function calculateMetrics(
  config: BacktestConfig,
  equityCurve: EquityPoint[],
  trades: Trade[]
): BacktestMetrics {
  const initialEquity = config.initialCapital;
  const finalEquity = equityCurve[equityCurve.length - 1]?.equity || initialEquity;
  const totalReturn = (finalEquity - initialEquity) / initialEquity;

  const days = equityCurve.length;
  const years = days / 252;
  const cagr = years > 0 ? Math.pow(1 + totalReturn, 1 / years) - 1 : totalReturn;

  // Calculate returns
  const returns = equityCurve.slice(1).map((point, i) => ({
    date: point.date,
    return: (point.equity - equityCurve[i].equity) / equityCurve[i].equity,
  }));

  const avgReturn = returns.reduce((sum, r) => sum + r.return, 0) / returns.length;
  const variance = returns.reduce((sum, r) => sum + Math.pow(r.return - avgReturn, 2), 0) / returns.length;
  const stdDev = Math.sqrt(variance);
  const annualizedVol = stdDev * Math.sqrt(252);

  // Sharpe ratio (assuming 6% risk-free rate)
  const riskFreeRate = 0.06;
  const sharpeRatio = annualizedVol > 0
    ? (cagr - riskFreeRate) / annualizedVol
    : 0;

  // Sortino ratio (downside deviation only)
  const downsideReturns = returns.filter(r => r.return < 0).map(r => r.return);
  const downsideDev = downsideReturns.length > 0
    ? Math.sqrt(downsideReturns.reduce((sum, r) => sum + r * r, 0) / downsideReturns.length) * Math.sqrt(252)
    : 0;
  const sortinoRatio = downsideDev > 0 ? (cagr - riskFreeRate) / downsideDev : 0;

  // Max drawdown
  let maxDrawdown = 0;
  let peak = initialEquity;
  let maxDDDuration = 0;
  let currentDDStart = 0;

  for (let i = 0; i < equityCurve.length; i++) {
    const point = equityCurve[i];
    if (point.equity > peak) {
      peak = point.equity;
      currentDDStart = i;
    }
    const drawdown = (peak - point.equity) / peak;
    if (drawdown > maxDrawdown) {
      maxDrawdown = drawdown;
      maxDDDuration = i - currentDDStart;
    }
  }

  // Calmar ratio
  const calmarRatio = maxDrawdown > 0 ? cagr / maxDrawdown : 0;

  // Trade statistics
  const closedTrades = trades.filter(t => t.type === 'exit');
  const winningTrades = closedTrades.filter(t => (t.pnl || 0) > 0);
  const losingTrades = closedTrades.filter(t => (t.pnl || 0) <= 0);

  const winRate = closedTrades.length > 0 ? winningTrades.length / closedTrades.length : 0;

  const grossProfit = winningTrades.reduce((sum, t) => sum + (t.pnl || 0), 0);
  const grossLoss = Math.abs(losingTrades.reduce((sum, t) => sum + (t.pnl || 0), 0));
  const profitFactor = grossLoss > 0 ? grossProfit / grossLoss : grossProfit > 0 ? Infinity : 0;

  const avgWin = winningTrades.length > 0
    ? winningTrades.reduce((sum, t) => sum + (t.return || 0), 0) / winningTrades.length
    : 0;
  const avgLoss = losingTrades.length > 0
    ? losingTrades.reduce((sum, t) => sum + (t.return || 0), 0) / losingTrades.length
    : 0;

  const avgTradeReturn = closedTrades.length > 0
    ? closedTrades.reduce((sum, t) => sum + (t.return || 0), 0) / closedTrades.length
    : 0;

  const avgDuration = closedTrades.length > 0
    ? closedTrades.reduce((sum, t) => sum + (t.duration || 0), 0) / closedTrades.length
    : 0;

  // VaR (95%) - simplified
  const sortedReturns = [...returns].sort((a, b) => a.return - b.return);
  const varIndex = Math.floor(sortedReturns.length * 0.05);
  const var95 = sortedReturns[varIndex]?.return || 0;

  const tradeStats: TradeStats = {
    totalTrades: closedTrades.length,
    winningTrades: winningTrades.length,
    losingTrades: losingTrades.length,
    averageWin: avgWin,
    averageLoss: Math.abs(avgLoss),
    largestWin: winningTrades.length > 0 ? Math.max(...winningTrades.map(t => t.return || 0)) : 0,
    largestLoss: Math.abs(losingTrades.length > 0 ? Math.min(...losingTrades.map(t => t.return || 0)) : 0),
    averageTrade: avgTradeReturn,
    averageHoldTime: avgDuration,
    maxHoldTime: closedTrades.length > 0 ? Math.max(...closedTrades.map(t => t.duration || 0)) : 0,
    minHoldTime: closedTrades.length > 0 ? Math.min(...closedTrades.map(t => t.duration || 30)) : 0,
    consecutiveWins: 8,  // Simplified
    consecutiveLosses: 4, // Simplified
  };

  return {
    totalReturn,
    cagr,
    annualizedReturn: cagr,
    annualizedVolatility: annualizedVol,
    volatility: annualizedVol,
    sharpeRatio,
    sortinoRatio,
    maxDrawdown: -maxDrawdown, // Negative for consistency
    maxDrawdownDuration: maxDDDuration,
    calmarRatio,
    var95,
    totalTrades: closedTrades.length,
    winningTrades: winningTrades.length,
    losingTrades: losingTrades.length,
    winRate,
    profitFactor,
    avgTradeReturn,
    avgWin,
    avgLoss,
    largestWin: winningTrades.length > 0 ? Math.max(...winningTrades.map(t => t.return || 0)) : 0,
    largestLoss: losingTrades.length > 0 ? Math.min(...losingTrades.map(t => t.return || 0)) : 0,
    avgTradeDuration: avgDuration,
    maxConsecutiveWins: 8,  // Simplified
    maxConsecutiveLosses: 4, // Simplified
  };
}

export function calculateTradeStats(metrics: BacktestMetrics, trades: Trade[]): TradeStats {
  const closedTrades = trades.filter(t => t.type === 'exit');
  const winningTrades = closedTrades.filter(t => (t.pnl || 0) > 0);
  const losingTrades = closedTrades.filter(t => (t.pnl || 0) <= 0);

  const avgWin = winningTrades.length > 0
    ? winningTrades.reduce((sum, t) => sum + (t.return || 0), 0) / winningTrades.length
    : 0;
  const avgLoss = losingTrades.length > 0
    ? losingTrades.reduce((sum, t) => sum + (t.return || 0), 0) / losingTrades.length
    : 0;
  const avgDuration = closedTrades.length > 0
    ? closedTrades.reduce((sum, t) => sum + (t.duration || 0), 0) / closedTrades.length
    : 0;

  return {
    totalTrades: closedTrades.length,
    winningTrades: winningTrades.length,
    losingTrades: losingTrades.length,
    averageWin: avgWin,
    averageLoss: Math.abs(avgLoss),
    largestWin: winningTrades.length > 0 ? Math.max(...winningTrades.map(t => t.return || 0)) : 0,
    largestLoss: Math.abs(losingTrades.length > 0 ? Math.min(...losingTrades.map(t => t.return || 0)) : 0),
    averageTrade: closedTrades.length > 0
      ? closedTrades.reduce((sum, t) => sum + (t.return || 0), 0) / closedTrades.length
      : 0,
    averageHoldTime: avgDuration,
    maxHoldTime: closedTrades.length > 0 ? Math.max(...closedTrades.map(t => t.duration || 0)) : 0,
    minHoldTime: closedTrades.length > 0 ? Math.min(...closedTrades.map(t => t.duration || 30)) : 0,
    consecutiveWins: 8,
    consecutiveLosses: 4,
  };
}

// ============================================================================
// Monthly Returns Generation
// ============================================================================

function generateMonthlyReturns(equityCurve: EquityPoint[]): MonthlyReturn[] {
  const monthlyData = new Map<string, { start: number; end: number }>();

  for (const point of equityCurve) {
    const key = point.date.substring(0, 7); // YYYY-MM
    if (!monthlyData.has(key)) {
      monthlyData.set(key, { start: point.equity, end: point.equity });
    }
    monthlyData.get(key)!.end = point.equity;
  }

  return Array.from(monthlyData.entries()).map(([key, data]) => ({
    year: parseInt(key.substring(0, 4)),
    month: parseInt(key.substring(5, 7)),
    return: (data.end - data.start) / data.start,
  }));
}

// ============================================================================
// Main Mock API Functions
// ============================================================================

/**
 * Run a mock backtest with realistic data
 */
export async function runMockBacktest(config: BacktestConfig): Promise<BacktestResult> {
  // Simulate API delay
  await new Promise(resolve => setTimeout(resolve, 1500));

  const startPrice = config.assetType === 'option' ? 150 : 22000; // Option premium vs Index
  const priceData = generatePriceData(config.dateRange.start, config.dateRange.end, startPrice);

  const trades = generateTrades(config, priceData, config.initialCapital);
  const equityCurve = generateEquityCurve(config, priceData, trades);
  const metrics = calculateMetrics(config, equityCurve, trades);
  const monthlyReturns = generateMonthlyReturns(equityCurve);

  // Generate benchmark comparison (NIFTY)
  const benchmarkData = generatePriceData(config.dateRange.start, config.dateRange.end, 18000);
  const benchmarkReturn = (benchmarkData[benchmarkData.length - 1].close - benchmarkData[0].close) / benchmarkData[0].close;

  // Generate Monte Carlo and Walk Forward
  const tempResult: BacktestResult = {
    runId: generateId(),
    config,
    equityCurve,
    trades,
    metrics,
    monthlyReturns,
    benchmarkComparison: {
      symbol: 'NIFTY50',
      totalReturn: benchmarkReturn,
      equityCurve: benchmarkData.map(p => ({ date: p.date, value: p.close / benchmarkData[0].close * config.initialCapital })),
    },
    createdAt: new Date().toISOString(),
    status: 'completed',
    stats: calculateTradeStats(metrics, trades),
    monteCarlo: generateMonteCarlo({ runId: '', config, equityCurve, trades, metrics, monthlyReturns, createdAt: '', status: 'completed' }),
    walkForward: generateWalkForward({ runId: '', config, equityCurve, trades, metrics, monthlyReturns, createdAt: '', status: 'completed' }),
  };

  // Recalculate monte carlo and walk forward with full result
  tempResult.monteCarlo = generateMonteCarlo(tempResult);
  tempResult.walkForward = generateWalkForward(tempResult);

  return tempResult;
}

/**
 * Generate Monte Carlo simulation results
 */
export function generateMonteCarlo(result: BacktestResult, simulations: number = 1000): MonteCarloResult {
  const paths: number[][] = [];
  const finalValues: number[] = [];

  for (let i = 0; i < simulations; i++) {
    const path = [result.config.initialCapital];
    let equity = result.config.initialCapital;

    // Reshuffle trade returns
    const shuffledReturns = [...result.trades]
      .filter(t => t.type === 'exit')
      .map(t => t.return || 0)
      .sort(() => Math.random() - 0.5);

    for (const ret of shuffledReturns) {
      equity *= (1 + ret);
      path.push(equity);
    }

    paths.push(path);
    finalValues.push(equity);
  }

  finalValues.sort((a, b) => a - b);

  const medianIndex = Math.floor(simulations / 2);
  const p5Index = Math.floor(simulations * 0.05);
  const p95Index = Math.floor(simulations * 0.95);

  const profitable = finalValues.filter(v => v > result.config.initialCapital).length;
  const ruined = finalValues.filter(v => v < result.config.initialCapital * 0.5).length;

  return {
    simulations,
    probabilityOfProfit: profitable / simulations,
    probabilityOfRuin: ruined / simulations,
    medianFinalEquity: finalValues[medianIndex],
    worstCase: finalValues[p5Index],
    bestCase: finalValues[p95Index],
    confidenceInterval: {
      lower: finalValues[p5Index],
      upper: finalValues[p95Index],
    },
    equityCurves: {
      median: paths[medianIndex],
      p5: paths[p5Index],
      p95: paths[p95Index],
    },
  };
}

/**
 * Generate walk-forward analysis
 */
export function generateWalkForward(result: BacktestResult): WalkForwardResult {
  // Simplified walk-forward (in real impl, would re-run backtest on windows)
  const windows: WalkForwardWindow[] = [];
  const numWindows = 4;

  for (let i = 0; i < numWindows; i++) {
    const inSampleSharpe = result.metrics.sharpeRatio * randomBetween(0.9, 1.1);
    const degradation = randomBetween(0.10, 0.25);
    const outSampleSharpe = inSampleSharpe * (1 - degradation);

    windows.push({
      window: i + 1,
      inSampleStart: addDays(result.config.dateRange.start, i * 180),
      inSampleEnd: addDays(result.config.dateRange.start, i * 180 + 120),
      outOfSampleStart: addDays(result.config.dateRange.start, i * 180 + 120),
      outOfSampleEnd: addDays(result.config.dateRange.start, i * 180 + 180),
      inSampleSharpe,
      outOfSampleSharpe: outSampleSharpe,
      degradation,
    });
  }

  const avgDegradation = windows.reduce((sum, w) => sum + w.degradation, 0) / windows.length;

  return {
    windows,
    avgInSampleSharpe: windows.reduce((sum, w) => sum + w.inSampleSharpe, 0) / windows.length,
    avgOutOfSampleSharpe: windows.reduce((sum, w) => sum + w.outOfSampleSharpe, 0) / windows.length,
    avgDegradation,
    isRobust: avgDegradation < 0.30,
  };
}

/**
 * Get list of mock scenarios
 */
export function getMockScenarios() {
  return MOCK_SCENARIOS;
}

/**
 * Get a specific mock scenario
 */
export function getMockScenario(id: string) {
  return MOCK_SCENARIOS.find(s => s.id === id);
}

// ============================================================================
// Simulated API Client
// ============================================================================

export const mockBacktestAPI = {
  runBacktest: runMockBacktest,
  getMonteCarlo: generateMonteCarlo,
  getWalkForward: generateWalkForward,
  getScenarios: getMockScenarios,
  getScenario: getMockScenario,
};

export default mockBacktestAPI;
