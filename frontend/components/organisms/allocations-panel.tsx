import { Decimal, Percent } from "@/components/atoms/number";
import { TableCell, TableRow } from "@/components/atoms/table";
import { DataTable } from "@/components/molecules/data-table";
import { QuerySection } from "@/components/molecules/query-section";
import { PLACEHOLDER } from "@/lib/format";
import { useHatAllocations, useSharedStale, type HatAllocations } from "@/lib/queries/hat";
import { formatIstDate } from "@/lib/time";

const COLUMNS = [
  { label: "Bot" },
  { label: "Weight", numeric: true },
  { label: "Regime ×", numeric: true },
  { label: "Health ×", numeric: true },
  { label: "Capital", numeric: true },
  { label: "Risk / trade", numeric: true },
];

function AllocationsTable({ allocations }: Readonly<{ allocations: HatAllocations }>) {
  return (
    <DataTable columns={COLUMNS}>
      {allocations.rows.map((row) => (
        <TableRow key={row.bot_id}>
          <TableCell>{row.bot}</TableCell>
          <TableCell numeric>
            <Percent value={row.final_weight} />
          </TableCell>
          <TableCell numeric>
            <Decimal value={row.regime_mult} />
          </TableCell>
          <TableCell numeric>
            <Decimal value={row.health_mult} />
          </TableCell>
          <TableCell numeric title={row.currency}>
            <Decimal value={row.capital} />
          </TableCell>
          <TableCell numeric>
            <Percent value={row.risk_pct} />
          </TableCell>
        </TableRow>
      ))}
    </DataTable>
  );
}

/** The latest allocation: each bot's weight, multipliers, capital and risk per trade. */
export function AllocationsPanel({ className }: Readonly<{ className?: string }>) {
  const allocations = useHatAllocations();
  const sharedStale = useSharedStale() !== null;
  return (
    <QuerySection
      sharedStale={sharedStale}
      title="Allocations"
      query={allocations}
      isEmpty={(data) => data.rows.length === 0}
      empty="No allocation yet."
      meta={(data) => (data.trade_date ? `for ${formatIstDate(data.trade_date)}` : PLACEHOLDER)}
      className={className}
    >
      {(data) => <AllocationsTable allocations={data} />}
    </QuerySection>
  );
}
