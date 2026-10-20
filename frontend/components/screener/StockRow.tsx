import { memo } from "react";
import { ExternalLink, TrendingDown, TrendingUp } from "lucide-react";
import type { ScreenerRow } from "@/lib/queries/screener";
import type { Quote } from "@/lib/realtime/quote-store";
import { getTradingViewUrl } from "@/lib/tradingview";
import {
  formatRowCells,
  getMacdClassName,
  getRsiClassName,
  toNumber,
  withLiveQuote,
} from "./screener-view";
import { useFlash } from "./use-flash";

interface StockRowProps {
  row: ScreenerRow;
  /** The quote that wins; quotes are stable references until they change. */
  live: Quote | undefined;
  /** Fills the fields `live` lacks. */
  fallback: Quote | undefined;
  onOpen: (symbol: string) => void;
}

function openTradingView(symbol: string) {
  window.open(getTradingViewUrl(symbol), "_blank", "noopener,noreferrer");
}

/** One screener row with its live values; price, change and volume flash when they move. */
export const StockRow = memo(function StockRow({
  row,
  live,
  fallback,
  onOpen,
}: Readonly<StockRowProps>) {
  const stock = withLiveQuote(row, live, fallback);
  const cells = formatRowCells(stock);
  const priceFlash = useFlash(toNumber(stock.price));
  const changeFlash = useFlash(toNumber(stock.change));
  const volumeFlash = useFlash(toNumber(stock.volume));
  const rsiClassName = getRsiClassName(toNumber(stock.rsi));
  const macdClassName = getMacdClassName(toNumber(stock.macd));

  return (
    <tr
      onClick={() => onOpen(stock.symbol)}
      className="cursor-pointer hover:bg-surface transition-colors border-b border-border"
    >
      <td className="py-2 pl-4">
        <div className="flex items-center gap-1">
          <div className="font-medium">{stock.symbol}</div>
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              openTradingView(stock.symbol);
            }}
            className="text-foreground-muted hover:text-foreground"
            title="Open on TradingView"
            aria-label={`Open ${stock.symbol} on TradingView`}
          >
            <ExternalLink className="h-3.5 w-3.5" />
          </button>
        </div>
        <div className="text-xs text-foreground-muted truncate max-w-[120px]">{stock.name}</div>
      </td>
      <td className="py-2">
        <span className="text-xs px-1.5 py-0.5 rounded bg-background-tertiary">{stock.sector}</span>
      </td>
      <td className="py-2 text-right tabular-nums">
        <span className={priceFlash}>{cells.price}</span>
      </td>
      <td className={`py-2 text-right tabular-nums ${cells.isUp ? "text-profit" : "text-loss"}`}>
        <div className="flex items-center justify-end gap-1">
          {cells.isUp ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
          <span className={changeFlash}>{cells.change}</span>
        </div>
      </td>
      <td className="py-2 text-right tabular-nums text-foreground-muted">
        <span className={volumeFlash}>{cells.volume}</span>
      </td>
      <td className="py-2 text-right tabular-nums text-foreground-muted">{cells.marketCap}</td>
      <td className={`py-2 text-right tabular-nums ${rsiClassName}`}>{cells.rsi}</td>
      <td className={`py-2 pr-4 text-right tabular-nums ${macdClassName}`}>{cells.macd}</td>
    </tr>
  );
});
