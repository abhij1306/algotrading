import { memo } from "react";
import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
import type { ScreenerRow } from "@/lib/queries/screener";
import type { QuotePair, SortField, SortState } from "./screener-view";
import { StockRow } from "./StockRow";

const SortIcon = memo(function SortIcon({
  field,
  sort,
}: Readonly<{ field: SortField; sort: SortState }>) {
  if (sort.field !== field) {
    return <ArrowUpDown className="w-3 h-3 opacity-30" />;
  }
  return sort.direction === "asc" ? (
    <ArrowUp className="w-3 h-3" />
  ) : (
    <ArrowDown className="w-3 h-3" />
  );
});

type SortColumn = { field: SortField; label: string; thClassName: string; divClassName: string };

const RIGHT_TH = "py-2 text-right font-normal cursor-pointer hover:text-foreground";
const RIGHT_DIV = "flex items-center justify-end gap-1";

const SYMBOL_COLUMN: SortColumn = {
  field: "symbol",
  label: "Symbol",
  thClassName: "py-2 pl-4 text-left font-normal cursor-pointer hover:text-foreground",
  divClassName: "flex items-center gap-1",
};

/** The sortable columns after Sector, in display order. */
const NUMBER_COLUMNS: SortColumn[] = [
  { field: "price", label: "Price", thClassName: RIGHT_TH, divClassName: RIGHT_DIV },
  { field: "change", label: "Change", thClassName: RIGHT_TH, divClassName: RIGHT_DIV },
  { field: "volume", label: "Volume", thClassName: RIGHT_TH, divClassName: RIGHT_DIV },
  { field: "marketCap", label: "Mkt Cap", thClassName: RIGHT_TH, divClassName: RIGHT_DIV },
  { field: "rsi", label: "RSI", thClassName: RIGHT_TH, divClassName: RIGHT_DIV },
  {
    field: "macd",
    label: "MACD",
    thClassName: "py-2 pr-4 text-right font-normal cursor-pointer hover:text-foreground",
    divClassName: RIGHT_DIV,
  },
];

function SortHeader({
  column,
  sort,
  onSort,
}: Readonly<{ column: SortColumn; sort: SortState; onSort: (field: SortField) => void }>) {
  return (
    <th className={column.thClassName} onClick={() => onSort(column.field)}>
      <div className={column.divClassName}>
        {column.label} <SortIcon field={column.field} sort={sort} />
      </div>
    </th>
  );
}

interface ScreenerTableProps {
  rows: readonly ScreenerRow[];
  sort: SortState;
  onSort: (field: SortField) => void;
  /** A row's quotes in priority order. */
  quotesFor: (symbol: string) => QuotePair;
  onOpen: (symbol: string) => void;
}

/** The results table: server-sorted rows under clickable sort headers. */
export function ScreenerTable({
  rows,
  sort,
  onSort,
  quotesFor,
  onOpen,
}: Readonly<ScreenerTableProps>) {
  return (
    <table className="w-full">
      <thead className="sticky top-0 bg-surface border-b border-border text-xs">
        <tr>
          <SortHeader column={SYMBOL_COLUMN} sort={sort} onSort={onSort} />
          <th className="py-2 text-left font-normal">Sector</th>
          {NUMBER_COLUMNS.map((column) => (
            <SortHeader key={column.field} column={column} sort={sort} onSort={onSort} />
          ))}
        </tr>
      </thead>
      <tbody className="text-sm">
        {rows.map((row) => {
          const [live, fallback] = quotesFor(row.symbol);
          return (
            <StockRow key={row.symbol} row={row} live={live} fallback={fallback} onOpen={onOpen} />
          );
        })}
      </tbody>
    </table>
  );
}
