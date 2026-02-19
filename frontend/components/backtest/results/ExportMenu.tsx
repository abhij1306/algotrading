'use client';

import { Download, FileSpreadsheet, FileText, FileJson } from 'lucide-react';
import { Button } from '@/components/ui';
import { BacktestResult } from '@/lib/backtest/types';

interface ExportMenuProps {
  result: BacktestResult;
}

export function ExportMenu({ result }: ExportMenuProps) {
  const exportToCSV = () => {
    // Export trades to CSV
    const trades = result.trades.filter((t) => t.type === 'exit');
    const headers = ['Date', 'Symbol', 'Action', 'Entry Price', 'Exit Price', 'Quantity', 'P&L', 'Return %', 'Duration'];
    const rows = trades.map((t) => [
      t.date,
      t.symbol,
      t.action,
      t.entryPrice?.toFixed(2) || '',
      t.price.toFixed(2),
      t.quantity,
      t.pnl?.toFixed(2) || '0',
      ((t.return || 0) * 100).toFixed(2),
      t.duration?.toString() || '0',
    ]);

    const csv = [headers.join(','), ...rows.map((r) => r.join(','))].join('\n');
    downloadFile(csv, `backtest-trades-${result.runId}.csv`, 'text/csv');
  };

  const exportToJSON = () => {
    const data = {
      runId: result.runId,
      config: result.config,
      metrics: result.metrics,
      stats: result.stats,
      trades: result.trades,
    };
    const json = JSON.stringify(data, null, 2);
    downloadFile(json, `backtest-report-${result.runId}.json`, 'application/json');
  };

  const downloadFile = (content: string, filename: string, type: string) => {
    const blob = new Blob([content], { type });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };

  return (
    <div className="flex gap-2">
      <Button variant="outline" size="sm" onClick={exportToCSV}>
        <FileSpreadsheet className="w-4 h-4 mr-2" />
        CSV
      </Button>
      <Button variant="outline" size="sm" onClick={exportToJSON}>
        <FileJson className="w-4 h-4 mr-2" />
        JSON
      </Button>
    </div>
  );
}
