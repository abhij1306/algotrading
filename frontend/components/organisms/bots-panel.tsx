import { Badge } from "@/components/atoms/badge";
import { TableCell, TableRow } from "@/components/atoms/table";
import { DataTable } from "@/components/molecules/data-table";
import { noRows, QuerySection } from "@/components/molecules/query-section";
import { useHatStatus, type HatStatus } from "@/lib/queries/hat";

type Bot = HatStatus["bots"][number];

const COLUMNS = [{ label: "Bot" }, { label: "Mode" }, { label: "Lifecycle" }, { label: "State" }];

/** The mode the bot runs, why it differs from the one asked for, and any queued change. */
function modeLabel(bot: Bot): string {
  let label = bot.effective_mode;
  if (bot.effective_mode !== bot.execution_mode) {
    label += ` (asked ${bot.execution_mode}: ${bot.mode_reason ?? "?"})`;
  }
  if (bot.pending_mode !== null) {
    label += `; ${bot.pending_mode} from ${bot.pending_from ?? "?"}`;
  }
  return label;
}

function BotRow({ bot }: Readonly<{ bot: Bot }>) {
  return (
    <TableRow>
      <TableCell>{bot.name}</TableCell>
      <TableCell>{modeLabel(bot)}</TableCell>
      <TableCell>{bot.lifecycle_state}</TableCell>
      <TableCell>
        <Badge variant={bot.manage_only ? "warning" : "profit"}>
          {bot.manage_only ? `manage-only: ${bot.reasons.join(", ")}` : "active"}
        </Badge>
      </TableCell>
    </TableRow>
  );
}

/** Every bot with its effective mode, lifecycle and manage-only state. */
export function BotsPanel() {
  const status = useHatStatus();
  const bots = { ...status, data: status.data?.bots };
  return (
    <QuerySection title="Bots" query={bots} isEmpty={noRows} empty="No bots.">
      {(data) => (
        <DataTable columns={COLUMNS}>
          {data.map((bot) => (
            <BotRow key={bot.id} bot={bot} />
          ))}
        </DataTable>
      )}
    </QuerySection>
  );
}
