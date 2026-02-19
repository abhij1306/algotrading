'use client';

import { useState, useMemo } from 'react';
import { Search, ChevronDown } from 'lucide-react';
import { Input, Badge } from '@/components/ui';
import { cn } from '@/lib/utils';
import { AssetType } from '@/lib/backtest/types';
import { POPULAR_SYMBOLS, INDEX_UNIVERSES } from '@/lib/backtest/constants';

interface SymbolSelectorProps {
  assetType: AssetType;
  selectedSymbol: string;
  selectedUniverse: string;
  onSymbolChange: (symbol: string) => void;
  onUniverseChange: (universe: string) => void;
}

export function SymbolSelector({
  assetType,
  selectedSymbol,
  selectedUniverse,
  onSymbolChange,
  onUniverseChange,
}: SymbolSelectorProps) {
  const [searchQuery, setSearchQuery] = useState('');
  const [isOpen, setIsOpen] = useState(false);

  const filteredSymbols = useMemo(() => {
    if (!searchQuery) return POPULAR_SYMBOLS.slice(0, 8);
    const query = searchQuery.toLowerCase();
    return POPULAR_SYMBOLS.filter(
      (s) =>
        s.symbol.toLowerCase().includes(query) ||
        s.name.toLowerCase().includes(query)
    ).slice(0, 8);
  }, [searchQuery]);

  if (assetType === 'index') {
    return (
      <div className="space-y-2">
        <select
          value={selectedUniverse}
          onChange={(e) => onUniverseChange(e.target.value)}
          className="w-full px-3 py-2 rounded-md border border-input bg-background text-sm"
        >
          {INDEX_UNIVERSES.map((universe) => (
            <option key={universe.code} value={universe.code}>
              {universe.name} - {universe.description}
            </option>
          ))}
        </select>
        <p className="text-xs text-muted-foreground">
          Uses historical constituent data for accurate reconstruction
        </p>
      </div>
    );
  }

  const selectedSymbolData = POPULAR_SYMBOLS.find((s) => s.symbol === selectedSymbol);

  return (
    <div className="space-y-2">
      {/* Search Input */}
      <div className="relative">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
        <Input
          placeholder="Search symbol..."
          value={searchQuery}
          onChange={(e) => {
            setSearchQuery(e.target.value);
            setIsOpen(true);
          }}
          onFocus={() => setIsOpen(true)}
          className="pl-9"
        />
      </div>

      {/* Dropdown Results */}
      {isOpen && (
        <div className="relative">
          <div className="absolute z-50 w-full mt-1 bg-popover border rounded-md shadow-lg max-h-60 overflow-auto">
            {filteredSymbols.map((symbol) => (
              <button
                key={symbol.symbol}
                onClick={() => {
                  onSymbolChange(symbol.symbol);
                  setSearchQuery(symbol.symbol);
                  setIsOpen(false);
                }}
                className={cn(
                  'w-full px-3 py-2 flex items-center justify-between hover:bg-accent',
                  selectedSymbol === symbol.symbol && 'bg-accent'
                )}
              >
                <div className="flex items-center gap-2">
                  <span className="font-semibold text-sm">{symbol.symbol}</span>
                  <span className="text-xs text-muted-foreground truncate max-w-[150px]">
                    {symbol.name}
                  </span>
                </div>
                <Badge variant={symbol.type === 'INDEX' ? 'secondary' : 'outline'} className="text-[10px]">
                  {symbol.type}
                </Badge>
              </button>
            ))}
            {filteredSymbols.length === 0 && (
              <div className="px-3 py-2 text-sm text-muted-foreground">
                No symbols found
              </div>
            )}
          </div>
        </div>
      )}

      {/* Selected Symbol Display */}
      {selectedSymbolData && (
        <div className="flex items-center justify-between p-2 bg-muted rounded-md">
          <div>
            <div className="font-semibold">{selectedSymbolData.symbol}</div>
            <div className="text-xs text-muted-foreground">{selectedSymbolData.name}</div>
          </div>
          <div className="text-right">
            <div className="text-xs text-muted-foreground">Sector</div>
            <div className="text-xs font-medium">{selectedSymbolData.sector}</div>
          </div>
        </div>
      )}

      {/* Quick Select */}
      <div className="flex flex-wrap gap-1">
        <span className="text-xs text-muted-foreground mr-1">Quick:</span>
        {POPULAR_SYMBOLS.slice(0, 5).map((symbol) => (
          <button
            key={symbol.symbol}
            onClick={() => {
              onSymbolChange(symbol.symbol);
              setSearchQuery(symbol.symbol);
            }}
            className={cn(
              'text-xxs px-2 py-0.5 rounded border transition-colors',
              selectedSymbol === symbol.symbol
                ? 'bg-primary text-primary-foreground border-primary'
                : 'bg-background hover:bg-accent'
            )}
          >
            {symbol.symbol}
          </button>
        ))}
      </div>
    </div>
  );
}
