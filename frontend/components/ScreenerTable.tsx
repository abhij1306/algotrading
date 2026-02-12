'use client'

import { useState } from 'react'
import { cn, formatPrice, formatPercent } from '@/lib/utils'
import { TrendingUp, TrendingDown, Minus, ExternalLink } from 'lucide-react'

// Types
interface Stock {
  symbol: string
  close: number
  volume: number
  ema20: number
  ema50: number
  atr_pct: number
  rsi: number
  vol_percentile: number
  change_pct?: number
  intraday_score?: number
  swing_score?: number
  is_20d_breakout: boolean
  trend_7d?: number
  trend_30d?: number
  macd: number
  macd_signal: number
  adx: number
  stoch_k: number
  stoch_d: number
  bb_upper: number
  bb_middle: number
  bb_lower: number
  net_income?: number
  eps?: number
  roe?: number
  debt_to_equity?: number
  market_cap?: number
  pe_ratio?: number
  revenue?: number
}

interface ScreenerTableProps {
  data: Stock[]
  type: 'intraday' | 'swing' | 'positional'
  viewMode?: 'technical' | 'financial'
}

// Column definitions
const technicalColumns = [
  { key: 'symbol', label: 'Symbol', width: 'w-24' },
  { key: 'close', label: 'Price', width: 'w-20' },
  { key: 'change_pct', label: 'Change', width: 'w-20' },
  { key: 'rsi', label: 'RSI', width: 'w-16' },
  { key: 'ema20_50', label: 'EMA 20/50', width: 'w-24' },
  { key: 'atr_pct', label: 'ATR %', width: 'w-16' },
  { key: 'vol_percentile', label: 'Vol %', width: 'w-16' },
  { key: 'score', label: 'Score', width: 'w-16' },
  { key: 'trend', label: 'Trend', width: 'w-20' },
  { key: 'breakout', label: 'Breakout', width: 'w-20' },
]

const financialColumns = [
  { key: 'symbol', label: 'Symbol', width: 'w-24' },
  { key: 'close', label: 'Price', width: 'w-20' },
  { key: 'change_pct', label: 'Change', width: 'w-20' },
  { key: 'market_cap', label: 'Mkt Cap', width: 'w-24' },
  { key: 'pe_ratio', label: 'P/E', width: 'w-16' },
  { key: 'eps', label: 'EPS', width: 'w-16' },
  { key: 'roe', label: 'ROE', width: 'w-16' },
  { key: 'debt_to_equity', label: 'D/E', width: 'w-16' },
  { key: 'revenue', label: 'Revenue', width: 'w-24' },
]

// Helper functions
function getChangeClass(change: number): string {
  if (change > 0) return 'text-[var(--color-profit)]'
  if (change < 0) return 'text-[var(--color-loss)]'
  return 'text-[var(--text-secondary)]'
}

function getChangeBg(change: number): string {
  if (change > 0) return 'bg-[var(--color-profit-bg)]'
  if (change < 0) return 'bg-[var(--color-loss-bg)]'
  return 'bg-[var(--color-elevated)]'
}

function getRsiClass(rsi: number): string {
  if (rsi >= 70) return 'text-[var(--color-loss)]'
  if (rsi <= 30) return 'text-[var(--color-profit)]'
  return 'text-[var(--text-secondary)]'
}

function getTrendIcon(trend: number | undefined) {
  if (!trend) return <Minus className="w-3.5 h-3.5 text-[var(--text-tertiary)]" />
  if (trend > 0) return <TrendingUp className="w-3.5 h-3.5 text-[var(--color-profit)]" />
  return <TrendingDown className="w-3.5 h-3.5 text-[var(--color-loss)]" />
}

function getScoreColor(score: number | undefined): string {
  if (!score) return 'text-[var(--text-tertiary)]'
  if (score >= 70) return 'text-[var(--color-profit)]'
  if (score >= 50) return 'text-[var(--color-accent-yellow)]'
  return 'text-[var(--text-secondary)]'
}

export default function ScreenerTable({ data, type, viewMode = 'technical' }: ScreenerTableProps) {
  const [hoveredRow, setHoveredRow] = useState<string | null>(null)
  const columns = viewMode === 'financial' ? financialColumns : technicalColumns

  return (
    <div className="w-full overflow-auto">
      {/* Header */}
      <div className="grid gap-2 px-4 py-2 border-b border-[var(--border-subtle)] bg-[var(--color-elevated)] sticky top-0 z-10" style={{ gridTemplateColumns: `repeat(${columns.length}, minmax(0, 1fr))` }}>
        {columns.map((col) => (
          <div key={col.key} className={cn("text-xs font-semibold text-[var(--text-tertiary)] uppercase tracking-wide", col.width === 'w-24' ? 'text-left' : 'text-right')}>
            {col.label}
          </div>
        ))}
      </div>

      {/* Rows */}
      {data.map((stock) => (
        <div
          key={stock.symbol}
          className="grid gap-2 px-4 py-2.5 border-b border-[var(--border-subtle)] hover:bg-[var(--color-elevated)] transition-colors cursor-pointer items-center"
          style={{ gridTemplateColumns: `repeat(${columns.length}, minmax(0, 1fr))` }}
          onMouseEnter={() => setHoveredRow(stock.symbol)}
          onMouseLeave={() => setHoveredRow(null)}
          onClick={() => window.open(`https://www.tradingview.com/chart/?symbol=NSE:${stock.symbol}`, '_blank')}
        >
          {/* Symbol */}
          <div className="text-sm font-bold text-[var(--text-primary)] flex items-center gap-1.5">
            {stock.symbol}
            {hoveredRow === stock.symbol && (
              <ExternalLink className="w-3 h-3 text-[var(--text-tertiary)]" />
            )}
          </div>

          {/* Close Price */}
          <div className="text-right font-mono text-sm text-[var(--text-primary)]">
            {formatPrice(stock.close)}
          </div>

          {/* Change % */}
          <div className="text-right">
            <span className={cn("font-mono text-sm font-medium", getChangeClass(stock.change_pct || 0))}>
              {stock.change_pct ? formatPercent(stock.change_pct) : '-'}
            </span>
          </div>

          {/* View Mode: Financial */}
          {viewMode === 'financial' && (
            <>
              <div className="text-right font-mono text-sm text-[var(--text-secondary)]">
                {stock.market_cap ? formatPrice(stock.market_cap) : '-'}
              </div>
              <div className="text-right font-mono text-sm text-[var(--text-secondary)]">
                {stock.pe_ratio ? stock.pe_ratio.toFixed(1) : '-'}
              </div>
              <div className="text-right font-mono text-sm text-[var(--text-secondary)]">
                {stock.eps ? formatPrice(stock.eps) : '-'}
              </div>
              <div className="text-right font-mono text-sm text-[var(--text-secondary)]">
                {stock.roe ? formatPercent(stock.roe) : '-'}
              </div>
              <div className="text-right font-mono text-sm text-[var(--text-secondary)]">
                {stock.debt_to_equity ? stock.debt_to_equity.toFixed(2) : '-'}
              </div>
              <div className="text-right font-mono text-sm text-[var(--text-secondary)]">
                {stock.revenue ? formatPrice(stock.revenue) : '-'}
              </div>
            </>
          )}

          {/* View Mode: Technical */}
          {viewMode === 'technical' && (
            <>
              {/* RSI */}
              <div className={cn("text-right font-mono text-sm font-medium", getRsiClass(stock.rsi))}>
                {stock.rsi ? stock.rsi.toFixed(1) : '-'}
              </div>

              {/* EMA 20/50 */}
              <div className="text-right font-mono text-xs text-[var(--text-secondary)]">
                {stock.ema20 && stock.ema50 ? (
                  <div className="flex flex-col items-end">
                    <span>{formatPrice(stock.ema20)}</span>
                    <span className={stock.ema20 > stock.ema50 ? 'text-[var(--color-profit)]' : 'text-[var(--color-loss)]'}>
                      {formatPrice(stock.ema50)}
                    </span>
                  </div>
                ) : '-'}
              </div>

              {/* ATR % */}
              <div className="text-right font-mono text-sm text-[var(--text-secondary)]">
                {stock.atr_pct ? stock.atr_pct.toFixed(1) : '-'}%
              </div>

              {/* Volume Percentile */}
              <div className="text-right font-mono text-sm text-[var(--text-secondary)]">
                {stock.vol_percentile ? Math.round(stock.vol_percentile).toString() : '-'}
              </div>

              {/* Score */}
              <div className={cn("text-right font-mono text-sm font-bold", getScoreColor(stock.intraday_score || stock.swing_score))}>
                {stock.intraday_score || stock.swing_score || '-'}
              </div>

              {/* Trend */}
              <div className="flex items-center justify-end gap-1">
                {getTrendIcon(stock.trend_7d)}
                {stock.trend_7d !== undefined && (
                  <span className="text-xs text-[var(--text-tertiary)]">
                    {stock.trend_7d > 0 ? '+' : ''}{stock.trend_7d.toFixed(1)}%
                  </span>
                )}
              </div>

              {/* Breakout */}
              <div className="text-right">
                {stock.is_20d_breakout ? (
                  <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-[var(--color-profit-bg)] text-[var(--color-profit)]">
                    Yes
                  </span>
                ) : (
                  <span className="text-xs text-[var(--text-tertiary)]">-</span>
                )}
              </div>
            </>
          )}
        </div>
      ))}
    </div>
  )
}
