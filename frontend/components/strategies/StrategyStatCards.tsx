import { Card, CardContent, Select } from "@/components/ui";
import type { StrategyStatus } from "@/lib/queries/strategies";

interface UniverseChoice {
  id: string;
  name: string;
}

interface StrategyStatCardsProps {
  universes: ReadonlyArray<UniverseChoice>;
  universe: string;
  status: StrategyStatus | null;
  signalCount: number;
  onChooseUniverse: (id: string) => void;
}

function StatCard({ label, value }: Readonly<{ label: string; value: string | number }>) {
  return (
    <Card>
      <CardContent className="p-3">
        <div className="text-xs uppercase text-foreground-muted">{label}</div>
        <div className="mt-1 text-sm font-semibold">{value}</div>
      </CardContent>
    </Card>
  );
}

/** The universe picker beside the scan, queue and position counts. */
export function StrategyStatCards({
  universes,
  universe,
  status,
  signalCount,
  onChooseUniverse,
}: Readonly<StrategyStatCardsProps>) {
  return (
    <div className="grid grid-cols-5 gap-3 border-b border-border px-4 py-3">
      <Card>
        <CardContent className="p-3">
          <Select
            label="Universe"
            value={universe}
            onChange={(e) => onChooseUniverse(e.target.value)}
          >
            {universes.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name || item.id}
              </option>
            ))}
          </Select>
        </CardContent>
      </Card>
      <StatCard label="Last Scan" value={status?.latest_scan_date ?? "Never"} />
      <StatCard label="Pending Entry" value={status?.pending_entries ?? 0} />
      <StatCard label="Open Positions" value={status?.open_positions ?? 0} />
      <StatCard label="Signal Count" value={signalCount} />
    </div>
  );
}
