'use client';

import { useState } from 'react';
import { ArrowUpRight, ArrowDownRight, ChevronLeft, ChevronRight } from 'lucide-react';
import { Card, Table, TableBody, TableCell, TableHead, TableHeader, TableRow, Button, Badge } from '@/components/ui';
import { Trade } from '@/lib/backtest/types';
import { formatCurrency, formatPercent } from '@/lib/utils';

interface TradeListProps {
  trades: Trade[];
}

const ITEMS_PER_PAGE = 10;

export function TradeList({ trades }: TradeListProps) {
  const [currentPage, setCurrentPage] = useState(1);
  const [filter, setFilter] = useState<'all' | 'winners' | 'losers'>('all');

  // Filter trades (only exit trades for the list)
  const exitTrades = trades.filter((t) => t.type === 'exit');

  const filteredTrades = exitTrades.filter((t) => {
    if (filter === 'winners') return (t.pnl || 0) > 0;
    if (filter === 'losers') return (t.pnl || 0) <= 0;
    return true;
  });

  const totalPages = Math.ceil(filteredTrades.length / ITEMS_PER_PAGE);
  const paginatedTrades = filteredTrades.slice(
    (currentPage - 1) * ITEMS_PER_PAGE,
    currentPage * ITEMS_PER_PAGE
  );

  const formatDate = (dateStr: string) => {
    return new Date(dateStr).toLocaleDateString('en-IN', {
      day: '2-digit',
      month: 'short',
    });
  };

  return (
    <Card className="p-4">
      {/* Filters */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex gap-2">
          {(['all', 'winners', 'losers'] as const).map((f) => (
            <Button
              key={f}
              variant={filter === f ? 'default' : 'outline'}
              size="sm"
              onClick={() => {
                setFilter(f);
                setCurrentPage(1);
              }}
            >
              {f.charAt(0).toUpperCase() + f.slice(1)}
              {f === 'all' && ` (${exitTrades.length})`}
              {f === 'winners' && ` (${exitTrades.filter((t) => (t.pnl || 0) > 0).length})`}
              {f === 'losers' && ` (${exitTrades.filter((t) => (t.pnl || 0) <= 0).length})`}
            </Button>
          ))}
        </div>
        <div className="text-sm text-muted-foreground">
          Showing {filteredTrades.length} trades
        </div>
      </div>

      {/* Table */}
      <div className="overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Date</TableHead>
              <TableHead>Symbol</TableHead>
              <TableHead>Action</TableHead>
              <TableHead className="text-right">Entry</TableHead>
              <TableHead className="text-right">Exit</TableHead>
              <TableHead className="text-right">P&L</TableHead>
              <TableHead className="text-right">Return</TableHead>
              <TableHead className="text-right">Duration</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {paginatedTrades.map((trade) => (
              <TableRow key={trade.id}>
                <TableCell className="text-xs">{formatDate(trade.date)}</TableCell>
                <TableCell className="font-medium">{trade.symbol}</TableCell>
                <TableCell>
                  <Badge variant={trade.action === 'BUY' ? 'default' : 'secondary'} className="text-xxs">
                    {trade.action}
                  </Badge>
                </TableCell>
                <TableCell className="text-right text-xs">
                  {trade.entryDate ? formatDate(trade.entryDate) : '-'}
                </TableCell>
                <TableCell className="text-right text-xs">
                  {trade.exitDate ? formatDate(trade.exitDate) : formatDate(trade.date)}
                </TableCell>
                <TableCell className="text-right">
                  <span className={(trade.pnl || 0) >= 0 ? 'text-green-500' : 'text-red-500'}>
                    {(trade.pnl || 0) >= 0 ? '+' : ''}
                    {formatCurrency(trade.pnl || 0)}
                  </span>
                </TableCell>
                <TableCell className="text-right">
                  <span className={`flex items-center justify-end gap-1 ${(trade.return || 0) >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                    {(trade.return || 0) >= 0 ? (
                      <ArrowUpRight className="w-3 h-3" />
                    ) : (
                      <ArrowDownRight className="w-3 h-3" />
                    )}
                    {Math.abs((trade.return || 0) * 100).toFixed(1)}%
                  </span>
                </TableCell>
                <TableCell className="text-right text-xs">
                  {trade.duration ? `${trade.duration}d` : '-'}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between mt-4">
          <Button
            variant="outline"
            size="sm"
            onClick={() => setCurrentPage((p) => Math.max(1, p - 1))}
            disabled={currentPage === 1}
          >
            <ChevronLeft className="w-4 h-4" />
          </Button>
          <span className="text-sm text-muted-foreground">
            Page {currentPage} of {totalPages}
          </span>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setCurrentPage((p) => Math.min(totalPages, p + 1))}
            disabled={currentPage === totalPages}
          >
            <ChevronRight className="w-4 h-4" />
          </Button>
        </div>
      )}
    </Card>
  );
}
