import { Decimal, Percent } from "@/components/atoms/number";
import { TableCell, TableRow } from "@/components/atoms/table";
import { DataTable } from "@/components/molecules/data-table";
import { QuerySection } from "@/components/molecules/query-section";
import { PLACEHOLDER } from "@/lib/format";
import { useHatAllocations, type HatAllocations } from "@/lib/queries/hat";
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
    <>
      <p className="mb-2 text-xs text-foreground-muted">
        Allocation of {allocations.trade_date ? formatIstDate(allocations.trade_date) : PLACEHOLDER}
      </p>
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
            <TableCell numeric>
              <Decimal value={row.capital} />
            </TableCell>
            <TableCell numeric>
              <Percent value={row.risk_pct} />
            </TableCell>
          </TableRow>
        ))}
      </DataTable>
    </>
  );
}

/** The latest allocation: each bot's weight, multipliers, capital and risk per trade. */
export function AllocationsPanel() {
  const allocations = useHatAllocations();
  return (
    <QuerySection
      title="Allocations"
      query={allocations}
      isEmpty={(data) => data.rows.length === 0}
      empty="No allocation yet."
    >
      {(data) => <AllocationsTable allocations={data} />}
    </QuerySection>
  );
}
