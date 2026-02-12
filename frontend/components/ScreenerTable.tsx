'use client'

import { useState } from 'react'
import { cn, formatPrice, formatPercent } from '@/lib/utils'
import { TrendingUp, TrendingDown, Minus, ExternalLink } from 'lucide-react'
import {
  Table, TableHeader, TableBody, TableRow, TableHead, TableCell
} from '@/components/ui/table'

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
  positional_score?: number
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
  type?: 'intraday' | 'swing' | 'positional'
  viewMode?: 'technical' | 'financial'
}

// Helper functions
function getChangeClass(change: number): string {
  if (change > 0) return 'text-[var(--color-profit)]'
  if (change < 0) return 'text-[var(--color-loss)]'
  return 'text-[var(--text-secondary)]'
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

export default function ScreenerTable({ data, type = 'intraday', viewMode = 'technical' }: ScreenerTableProps) {
  const [hoveredRow, setHoveredRow] = useState<string | null>(null)

  return (
    <Table>
      <TableHeader>
        <TableRow variant="ghost">
          <TableHead className="w-24 sticky left-0 bg-[var(--color-base)] z-20">Symbol</TableHead>
          <TableHead numeric className="w-20">Price</TableHead>
          <TableHead numeric className="w-20">Change</TableHead>
          {viewMode === 'financial' ? (
            <>
              <TableHead numeric className="w-24">Mkt Cap</TableHead>
              <TableHead numeric className="w-16">P/E</TableHead>
              <TableHead numeric className="w-16">EPS</TableHead>
              <TableHead numeric className="w-16">ROE</TableHead>
              <TableHead numeric className="w-16">D/E</TableHead>
              <TableHead numeric className="w-24">Revenue</TableHead>
            </>
          ) : (
            <>
              <TableHead numeric className="w-16">RSI</TableHead>
              <TableHead numeric className="w-28">EMA 20/50</TableHead>
              <TableHead numeric className="w-16">ATR %</TableHead>
              <TableHead numeric className="w-16">Vol %</TableHead>
              <TableHead numeric className="w-16">Score</TableHead>
              <TableHead numeric className="w-20">Trend</TableHead>
              <TableHead numeric className="w-20">Breakout</TableHead>
            </>
          )}
        </TableRow>
      </TableHeader>
      <TableBody>
        {data.map((stock) => (
          <TableRow
            key={stock.symbol}
            variant="ghost"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                window.open(`https://www.tradingview.com/chart/?symbol=NSE:${stock.symbol}`, '_blank')
              }
            }}
            onMouseEnter={() => setHoveredRow(stock.symbol)}
            onMouseLeave={() => setHoveredRow(null)}
            className="cursor-pointer"
            onClick={() => window.open(`https://www.tradingview.com/chart/?symbol=NSE:${stock.symbol}`, '_blank')}
          >
            {/* Symbol */}
            <TableCell className="font-semibold sticky left-0 bg-[var(--color-base)] z-10">
              <div className="flex items-center gap-1.5">
                {stock.symbol}
                {hoveredRow === stock.symbol && (
                  <ExternalLink className="w-3 h-3 text-[var(--text-tertiary)]" />
                )}
              </div>
            </TableCell>

            {/* Close Price */}
            <TableCell numeric>
              {formatPrice(stock.close)}
            </TableCell>

            {/* Change % */}
            <TableCell numeric>
              <span className={cn("font-medium", getChangeClass(stock.change_pct || 0))}>
                {stock.change_pct ? formatPercent(stock.change_pct) : '-'}
              </span>
            </TableCell>

            {/* View Mode: Financial */}
            {viewMode === 'financial' && (
              <>
                <TableCell numeric>
                  {stock.market_cap ? formatPrice(stock.market_cap) : '-'}
                </TableCell>
                <TableCell numeric>
                  {stock.pe_ratio ? stock.pe_ratio.toFixed(1) : '-'}
                </TableCell>
                <TableCell numeric>
                  {stock.eps ? formatPrice(stock.eps) : '-'}
                </TableCell>
                <TableCell numeric>
                  {stock.roe ? formatPercent(stock.roe) : '-'}
                </TableCell>
                <TableCell numeric>
                  {stock.debt_to_equity ? stock.debt_to_equity.toFixed(2) : '-'}
                </TableCell>
                <TableCell numeric>
                  {stock.revenue ? formatPrice(stock.revenue) : '-'}
                </TableCell>
              </>
            )}

            {/* View Mode: Technical */}
            {viewMode === 'technical' && (
              <>
                {/* RSI */}
                <TableCell numeric className={getRsiClass(stock.rsi)}>
                  {stock.rsi ? stock.rsi.toFixed(1) : '-'}
                </TableCell>

                {/* EMA 20/50 - All on one line */}
                <TableCell numeric>
                  {stock.ema20 && stock.ema50 ? (
                    <span>
                      <span className="text-[var(--text-primary)]">{formatPrice(stock.ema20)}</span>
                      <span className="mx-1 text-[var(--text-muted)]">/</span>
                      <span className={stock.ema20 > stock.ema50 ? 'text-[var(--color-profit)]' : 'text-[var(--color-loss)]'}>
                        {formatPrice(stock.ema50)}
                      </span>
                    </span>
                  ) : '-'}
                </TableCell>

                {/* ATR % */}
                <TableCell numeric>
                  {typeof stock.atr_pct === 'number' && Number.isFinite(stock.atr_pct) ? `${stock.atr_pct.toFixed(1)}%` : '-'}
                </TableCell>

                {/* Volume Percentile */}
                <TableCell numeric>
                  {stock.vol_percentile ? Math.round(stock.vol_percentile).toString() : '-'}
                </TableCell>

                {/* Score */}
                <TableCell numeric className={cn("font-bold", getScoreColor(stock.intraday_score || stock.swing_score || stock.positional_score || 0))}>
                  {type === 'intraday' ? stock.intraday_score : type === 'swing' ? stock.swing_score : stock.positional_score || '-'}
                </TableCell>

                {/* Trend */}
                <TableCell numeric>
                  <div className="flex items-center justify-end gap-1">
                    {getTrendIcon(stock.trend_7d)}
                    {stock.trend_7d !== undefined && (
                      <span>
                        {stock.trend_7d > 0 ? '+' : ''}{stock.trend_7d.toFixed(1)}%
                      </span>
                    )}
                  </div>
                </TableCell>

                {/* Breakout */}
                <TableCell numeric>
                  {stock.is_20d_breakout ? (
                    <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-[var(--color-profit-bg)] text-[var(--color-profit)]">
                      Yes
                    </span>
                  ) : (
                    <span>-</span>
                  )}
                </TableCell>
              </>
            )}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}
