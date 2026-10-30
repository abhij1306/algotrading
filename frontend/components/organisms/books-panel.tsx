import { useState } from "react";
import { Badge } from "@/components/atoms/badge";
import { Decimal } from "@/components/atoms/number";
import { TableCell, TableRow } from "@/components/atoms/table";
import { DataTable, RowOpener } from "@/components/molecules/data-table";
import { DetailList, Drawer } from "@/components/molecules/drawer";
import { LabelledValue } from "@/components/molecules/labelled-value";
import { QuerySection } from "@/components/molecules/query-section";
import { formatNumber, PLACEHOLDER } from "@/lib/format";
import { useHatBooks, useSharedStale, type HatBooks } from "@/lib/queries/hat";

type Book = HatBooks["books"][number];
type Metric = Book["metrics"][string];

const COLUMNS = [
  { label: "Book" },
  { label: "Equity", numeric: true },
  { label: "Day", numeric: true },
  { label: "Realised", numeric: true },
  { label: "Unrealised", numeric: true },
  { label: "Costs", numeric: true },
  { label: "Invested", numeric: true },
  { label: "Return", numeric: true },
  { label: "NIFTY 500 TRI", numeric: true },
  { label: "Trades", numeric: true },
  { label: "Exp R", numeric: true },
];

// The headline metrics shown in a book's details, as the backtest run page names them.
const METRICS: ReadonlyArray<readonly [string, string, "pct" | "num"]> = [
  ["win_pct", "Win rate", "pct"],
  ["avg_win_r", "Average win (R)", "num"],
  ["avg_loss_r", "Average loss (R)", "num"],
  ["cagr_pct", "CAGR", "pct"],
  ["benchmark_cagr_pct", "NIFTY 500 TRI CAGR", "pct"],
  ["max_dd_pct", "Max drawdown", "pct"],
  ["sharpe", "Sharpe", "num"],
  ["sortino", "Sortino", "num"],
  ["invested_pct", "Invested (mean)", "pct"],
  ["avg_open_positions", "Open positions (mean)", "num"],
  ["cost_share_pct", "Cost share", "pct"],
  ["max_consecutive_losses", "Losses in a row", "num"],
];

function percent(value: Metric | undefined): string {
  return typeof value === "number" ? `${formatNumber(value, 2)}%` : PLACEHOLDER;
}

function number(value: Metric | undefined): string {
  return typeof value === "number" ? formatNumber(value, 2) : PLACEHOLDER;
}

function BookDetails({ book: b, onClose }: Readonly<{ book: Book; onClose: () => void }>) {
  return (
    <Drawer
      title={`${b.bot} · ${b.kind}`}
      subtitle={`Since ${b.period_start}, start ${formatNumber(Number(b.start_capital), 2)}`}
      onClose={onClose}
    >
      <DetailList>
        {METRICS.map(([key, label, kind]) => (
          <LabelledValue key={key} label={label}>
            <span className="font-mono tabular-nums">
              {kind === "pct" ? percent(b.metrics[key]) : number(b.metrics[key])}
            </span>
          </LabelledValue>
        ))}
        {b.positions.map((p) => (
          <LabelledValue key={`open-${p.symbol}`} label={`Open ${p.symbol}`}>
            <span className="font-mono tabular-nums">
              {p.qty} @ <Decimal value={p.avg_entry} />,{" "}
              {p.open_r ? number(Number(p.open_r)) : PLACEHOLDER}R, <Decimal value={p.unrealised} />
            </span>
          </LabelledValue>
        ))}
        {b.trades.slice(-10).map((t) => (
          <LabelledValue
            key={`closed-${t.symbol}-${t.entry}`}
            label={`Closed ${t.symbol} ${t.exit}`}
          >
            <span className="font-mono tabular-nums">
              {number(Number(t.r))}R, <Decimal value={t.net_pnl} /> ({t.exit_reason})
            </span>
          </LabelledValue>
        ))}
      </DetailList>
    </Drawer>
  );
}

function BookRow({ book: b, onOpen }: Readonly<{ book: Book; onOpen: () => void }>) {
  return (
    <TableRow>
      <TableCell>
        <RowOpener label={`Details of the ${b.kind} book of ${b.bot}`} onOpen={onOpen}>
          {b.bot}
        </RowOpener>
        <Badge tone={b.kind === "live" ? "profit" : "mode"} className="ml-3 text-xs">
          {b.kind}
        </Badge>
      </TableCell>
      <TableCell numeric>
        <Decimal value={b.equity} />
      </TableCell>
      <TableCell numeric>
        <Decimal value={b.day_pnl} />
      </TableCell>
      <TableCell numeric>
        <Decimal value={b.realised} />
      </TableCell>
      <TableCell numeric>
        <Decimal value={b.unrealised} />
      </TableCell>
      <TableCell numeric>
        <Decimal value={b.fees} />
      </TableCell>
      <TableCell numeric>{percent(b.invested_pct)}</TableCell>
      <TableCell numeric>{percent(b.metrics["total_return_pct"])}</TableCell>
      <TableCell numeric>{percent(b.benchmark_return_pct)}</TableCell>
      <TableCell numeric>{number(b.metrics["trades"])}</TableCell>
      <TableCell numeric>{number(b.metrics["expectancy_r"])}</TableCell>
    </TableRow>
  );
}

/** Each live and paper book on the last stored session; a row opens its headline metrics. */
export function BooksPanel({ className }: Readonly<{ className?: string }>) {
  const books = useHatBooks();
  const sharedStale = useSharedStale() !== null;
  const [openKey, setOpenKey] = useState<string | null>(null);
  const open = books.data?.books.find((b) => `${b.bot}:${b.kind}` === openKey);
  return (
    <QuerySection
      sharedStale={sharedStale}
      title="Books"
      query={books}
      isEmpty={(data) => data.books.length === 0}
      empty="No book: no bot has an allocation yet."
      meta={(data) => `as of ${data.as_of}`}
      className={className}
    >
      {(data) => (
        <>
          <DataTable columns={COLUMNS}>
            {data.books.map((b) => (
              <BookRow
                key={`${b.bot}:${b.kind}`}
                book={b}
                onOpen={() => setOpenKey(`${b.bot}:${b.kind}`)}
              />
            ))}
          </DataTable>
          {open ? <BookDetails book={open} onClose={() => setOpenKey(null)} /> : null}
        </>
      )}
    </QuerySection>
  );
}
