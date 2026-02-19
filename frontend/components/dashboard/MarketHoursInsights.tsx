'use client';

import { memo } from 'react';
import { TrendingUp, TrendingDown, Activity } from 'lucide-react';
import { formatPercentage } from '@/lib/utils';
import { MarketInsights, StockMover, SectorData } from './InsightsPanel';

interface MarketHoursInsightsProps {
  data: MarketInsights;
}

const MoverRow = memo(function MoverRow({ stock }: { stock: StockMover }) {
  const isUp = stock.changePercent >= 0;
  return (
    <div className="flex items-center justify-between py-2 px-3 hover:bg-surface/50 rounded transition-colors">
      <div className="flex-1">
        <div className="font-medium text-sm">{stock.symbol}</div>
        <div className="text-xs text-foreground-muted truncate max-w-[120px]">{stock.name}</div>
      </div>
      <div className="text-right">
        <div className="text-sm tabular-nums">₹{stock.price.toFixed(2)}</div>
        <div className={`text-xs tabular-nums flex items-center justify-end gap-1 ${isUp ? 'text-profit' : 'text-loss'}`}>
          {isUp ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
          {formatPercentage(stock.changePercent)}
        </div>
      </div>
    </div>
  );
});

const SectorRow = memo(function SectorRow({ sector }: { sector: SectorData }) {
  const isUp = sector.changePercent >= 0;

  return (
    <div className="flex items-center justify-between py-2 px-3 hover:bg-surface/50 rounded transition-colors">
      <div className="flex-1">
        <div className="font-medium text-sm">{sector.symbol}</div>
        <div className="text-xs text-foreground-muted truncate max-w-[120px]">{sector.name}</div>
      </div>
      <div className="text-right">
        <div className="text-sm tabular-nums">{sector.value.toFixed(2)}</div>
        <div className={`text-xs tabular-nums font-medium ${isUp ? 'text-profit' : 'text-loss'}`}>
          {formatPercentage(sector.changePercent)}
        </div>
      </div>
    </div>
  );
});

export const MarketHoursInsights = memo(function MarketHoursInsights({ data }: MarketHoursInsightsProps) {
  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 p-4">
      {/* Top Gainers */}
      <div className="border border-border rounded-lg bg-background">
        <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-surface">
          <TrendingUp className="w-4 h-4 text-profit" />
          <span className="font-medium text-sm">Top Gainers</span>
          <span className="ml-auto text-xs text-foreground-muted">Fyers</span>
        </div>
        <div className="p-2 space-y-1">
          {data.topGainers.length > 0 ? (
            data.topGainers.map(stock => (
              <MoverRow key={stock.symbol} stock={stock} />
            ))
          ) : (
            <div className="p-4 text-center text-xs text-foreground-muted">No data</div>
          )}
        </div>
      </div>

      {/* Top Losers */}
      <div className="border border-border rounded-lg bg-background">
        <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-surface">
          <TrendingDown className="w-4 h-4 text-loss" />
          <span className="font-medium text-sm">Top Losers</span>
          <span className="ml-auto text-xs text-foreground-muted">Fyers</span>
        </div>
        <div className="p-2 space-y-1">
          {data.topLosers.length > 0 ? (
            data.topLosers.map(stock => (
              <MoverRow key={stock.symbol} stock={stock} />
            ))
          ) : (
            <div className="p-4 text-center text-xs text-foreground-muted">No data</div>
          )}
        </div>
      </div>

      {/* Sector Performance */}
      <div className="border border-border rounded-lg bg-background">
        <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-surface">
          <Activity className="w-4 h-4 text-foreground-muted" />
          <span className="font-medium text-sm">Sector Performance</span>
          <span className="ml-auto text-xs text-foreground-muted">Fyers</span>
        </div>
        <div className="p-2 space-y-1">
          {data.sectorPerformance.length > 0 ? (
            data.sectorPerformance.map(sector => (
              <SectorRow key={sector.name} sector={sector} />
            ))
          ) : (
            <div className="p-4 text-center text-xs text-foreground-muted">No data</div>
          )}
        </div>
      </div>
    </div>
  );
});
