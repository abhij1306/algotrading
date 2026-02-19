'use client';

import { memo } from 'react';
import { Globe, TrendingUp, TrendingDown, DollarSign } from 'lucide-react';
import { formatPercentage } from '@/lib/utils';
import { PostMarketData, IndexData, CommodityData } from './InsightsPanel';

interface PostMarketInsightsProps {
  data: PostMarketData;
}

const IndexRow = memo(function IndexRow({ index }: { index: IndexData }) {
  const hasValue = typeof index.value === 'number' && Number.isFinite(index.value);
  const hasChange = typeof index.changePercent === 'number' && Number.isFinite(index.changePercent);
  const isUp = hasChange ? (index.changePercent ?? 0) >= 0 : false;
  return (
    <div className="flex items-center justify-between py-2 px-3 hover:bg-surface/50 rounded transition-colors">
      <div className="font-medium text-sm">{index.name}</div>
      <div className="text-right">
        <div className="text-sm tabular-nums">{hasValue ? (index.value ?? 0).toFixed(2) : '—'}</div>
        <div className={`text-xs tabular-nums flex items-center justify-end gap-1 ${hasChange ? (isUp ? 'text-profit' : 'text-loss') : 'text-foreground-muted'}`}>
          {hasChange ? (
            <>
              {isUp ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
              {formatPercentage(index.changePercent)}
            </>
          ) : 'Unavailable'}
        </div>
      </div>
    </div>
  );
});

const CommodityCard = memo(function CommodityCard({ commodity }: { commodity: CommodityData }) {
  const hasPrice = typeof commodity.price === 'number' && Number.isFinite(commodity.price);
  const hasChange = typeof commodity.changePercent === 'number' && Number.isFinite(commodity.changePercent);
  const isUp = hasChange ? (commodity.changePercent ?? 0) >= 0 : false;
  return (
    <div className="p-3 rounded-lg border border-border bg-surface hover:bg-surface/80 transition-colors">
      <div className="text-xs text-foreground-muted mb-1">{commodity.name}</div>
      <div className="flex items-baseline justify-between">
        <div className="text-base font-semibold tabular-nums">{hasPrice ? `$${(commodity.price ?? 0).toFixed(2)}` : '—'}</div>
        <div className={`text-sm tabular-nums font-medium ${hasChange ? (isUp ? 'text-profit' : 'text-loss') : 'text-foreground-muted'}`}>
          {hasChange ? formatPercentage(commodity.changePercent) : 'Unavailable'}
        </div>
      </div>
    </div>
  );
});

export const PostMarketInsights = memo(function PostMarketInsights({ data }: PostMarketInsightsProps) {
  const usFearGreed = data.sentiment.usFearGreed;
  const indiaMMI = data.sentiment.indiaMMI;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 p-4">
      {/* US Indices */}
      <div className="border border-border rounded-lg bg-background">
        <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-surface">
          <Globe className="w-4 h-4 text-foreground-muted" />
          <span className="font-medium text-sm">US Markets</span>
          <span className="ml-auto text-xs text-foreground-muted">Yahoo</span>
        </div>
        <div className="p-2 space-y-1">
          {data.usIndices.length > 0 ? (
            data.usIndices.map(index => (
              <IndexRow key={index.name} index={index} />
            ))
          ) : (
            <div className="p-4 text-center text-xs text-foreground-muted">No data</div>
          )}
        </div>
      </div>

      {/* Commodities */}
      <div className="border border-border rounded-lg bg-background">
        <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-surface">
          <TrendingUp className="w-4 h-4 text-foreground-muted" />
          <span className="font-medium text-sm">Commodities</span>
          <span className="ml-auto text-xs text-foreground-muted">Yahoo</span>
        </div>
        <div className="p-2 space-y-2">
          {data.commodities.length > 0 ? (
            data.commodities.map(commodity => (
              <CommodityCard key={commodity.symbol} commodity={commodity} />
            ))
          ) : (
            <div className="p-4 text-center text-xs text-foreground-muted">No data</div>
          )}

          {/* Currency */}
          <div className="p-3 rounded-lg border border-border bg-surface">
            <div className="text-xs text-foreground-muted mb-1">USD/INR</div>
            <div className="flex items-baseline justify-between">
              <div className="text-base font-semibold tabular-nums">
                {typeof data.currency.rate === 'number' ? `₹${data.currency.rate.toFixed(2)}` : '—'}
              </div>
              <div className={`text-sm tabular-nums font-medium ${
                typeof data.currency.changePercent === 'number'
                  ? (data.currency.changePercent >= 0 ? 'text-profit' : 'text-loss')
                  : 'text-foreground-muted'
              }`}>
                {typeof data.currency.changePercent === 'number'
                  ? formatPercentage(data.currency.changePercent)
                  : 'Unavailable'}
              </div>
            </div>
          </div>

          {data.vix.length > 0 && (
            <div className="p-3 rounded-lg border border-border bg-surface">
              <div className="text-xs text-foreground-muted mb-2">Volatility Index</div>
              <div className="space-y-2">
                {data.vix.map((vixEntry) => (
                  <div key={vixEntry.name} className="flex items-center justify-between text-sm">
                    <span>{vixEntry.name}</span>
                    <span className="tabular-nums">
                      {typeof vixEntry.value === 'number' ? vixEntry.value.toFixed(2) : '—'}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Correlation Insights */}
      <div className="border border-border rounded-lg bg-background">
        <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-surface">
          <DollarSign className="w-4 h-4 text-foreground-muted" />
          <span className="font-medium text-sm">Sentiment</span>
        </div>
        <div className="p-2 space-y-2">
          <div className="p-3 rounded-lg border border-border bg-surface">
            <div className="text-xs text-foreground-muted mb-1">US Fear & Greed</div>
            <div className="flex items-center justify-between">
              <div className="text-sm font-medium">{usFearGreed.status}</div>
              <div className="text-sm tabular-nums">
                {typeof usFearGreed.score === 'number' ? usFearGreed.score : 'Unavailable'}
              </div>
            </div>
          </div>

          <div className="p-3 rounded-lg border border-border bg-surface">
            <div className="text-xs text-foreground-muted mb-1">India MMI</div>
            <div className="flex items-center justify-between">
              <div className="text-sm font-medium">{indiaMMI.status}</div>
              <div className="text-sm tabular-nums">
                {typeof indiaMMI.score === 'number' ? indiaMMI.score : 'Unavailable'}
              </div>
            </div>
            {indiaMMI.source && (
              <div className="mt-1 text-xs text-foreground-muted">Source: {indiaMMI.source}</div>
            )}
          </div>

          <div className="p-3 rounded-lg border border-border bg-surface">
            <div className="text-xs text-foreground-muted mb-1">Market Condition</div>
            <div className="text-sm">{data.condition?.status ?? 'Unavailable'}</div>
            {typeof data.condition?.adx === 'number' && (
              <div className="text-xs text-foreground-muted mt-1">ADX: {data.condition.adx.toFixed(2)}</div>
            )}
          </div>

          {data.timestamp && (
            <div className="text-xs text-foreground-muted px-1">
              Snapshot: {new Date(data.timestamp).toLocaleString('en-IN', { hour12: false })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
});
