'use client';

import { memo, useMemo, useCallback } from 'react';
import { Card } from '@/components/ui';
import { MonthlyReturn } from '@/lib/backtest/types';
import { cn } from '@/lib/utils';

interface MonthlyReturnsHeatmapProps {
  returns: MonthlyReturn[];
}

export const MonthlyReturnsHeatmap = memo(function MonthlyReturnsHeatmap({ returns }: MonthlyReturnsHeatmapProps) {
  // Group by year - memoized to avoid recalculation
  const byYear = useMemo(() => {
    return returns.reduce((acc, curr) => {
      if (!acc[curr.year]) acc[curr.year] = {};
      acc[curr.year][curr.month] = curr.return;
      return acc;
    }, {} as Record<number, Record<number, number>>);
  }, [returns]);

  const years = useMemo(() => Object.keys(byYear).map(Number).sort(), [byYear]);
  const months = useMemo(() => ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'], []);

  const getColorClass = useCallback((ret: number) => {
    const percent = ret * 100;
    if (percent >= 5) return 'bg-green-600';
    if (percent >= 3) return 'bg-green-500';
    if (percent >= 1) return 'bg-green-400';
    if (percent > 0) return 'bg-green-300';
    if (percent === 0) return 'bg-gray-500';
    if (percent > -1) return 'bg-red-300';
    if (percent > -3) return 'bg-red-400';
    if (percent > -5) return 'bg-red-500';
    return 'bg-red-600';
  }, []);

  const formatReturn = useCallback((ret: number) => {
    const percent = ret * 100;
    return `${percent >= 0 ? '+' : ''}${percent.toFixed(1)}%`;
  }, []);

  return (
    <Card className="p-4">
      <h3 className="text-sm font-semibold mb-4">Monthly Returns</h3>

      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr>
              <th className="text-left text-xs text-muted-foreground p-1">Year</th>
              {months.map((m) => (
                <th key={m} className="text-center text-xs text-muted-foreground p-1 w-12">
                  {m}
                </th>
              ))}
              <th className="text-right text-xs text-muted-foreground p-1">Total</th>
            </tr>
          </thead>
          <tbody>
            {years.map((year) => {
              const yearReturns = byYear[year];
              const yearTotal = Object.values(yearReturns).reduce((a, b) => a + b, 0);

              return (
                <tr key={year}>
                  <td className="text-sm font-medium p-1">{year}</td>
                  {Array.from({ length: 12 }, (_, i) => i + 1).map((month) => {
                    const ret = yearReturns[month];
                    return (
                      <td key={month} className="p-1">
                        {ret !== undefined ? (
                          <div
                            className={cn(
                              'w-full h-8 rounded flex items-center justify-center text-[10px] font-medium text-white',
                              getColorClass(ret)
                            )}
                            title={formatReturn(ret)}
                          >
                            {Math.abs(ret * 100) >= 10 ? formatReturn(ret) : ''}
                          </div>
                        ) : (
                          <div className="w-full h-8 rounded bg-muted" />
                        )}
                      </td>
                    );
                  })}
                  <td className="text-right text-sm font-medium p-1">
                    <span className={yearTotal >= 0 ? 'text-green-500' : 'text-red-500'}>
                      {formatReturn(yearTotal)}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Legend */}
      <div className="flex items-center gap-4 mt-4 text-xs">
        <div className="flex items-center gap-1">
          <div className="w-3 h-3 bg-green-600 rounded" />
          <span>&gt;+5%</span>
        </div>
        <div className="flex items-center gap-1">
          <div className="w-3 h-3 bg-green-400 rounded" />
          <span>+1-3%</span>
        </div>
        <div className="flex items-center gap-1">
          <div className="w-3 h-3 bg-gray-500 rounded" />
          <span>0%</span>
        </div>
        <div className="flex items-center gap-1">
          <div className="w-3 h-3 bg-red-400 rounded" />
          <span>-1-3%</span>
        </div>
        <div className="flex items-center gap-1">
          <div className="w-3 h-3 bg-red-600 rounded" />
          <span>&lt;-5%</span>
        </div>
      </div>
    </Card>
  );
});
