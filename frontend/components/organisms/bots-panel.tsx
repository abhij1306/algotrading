import { useState } from "react";
import { Badge } from "@/components/atoms/badge";
import { TableCell, TableRow } from "@/components/atoms/table";
import { DataTable, RowOpener } from "@/components/molecules/data-table";
import { DetailList, Drawer } from "@/components/molecules/drawer";
import { LabelledValue } from "@/components/molecules/labelled-value";
import { noRows, QuerySection, rowCount } from "@/components/molecules/query-section";
import { PLACEHOLDER } from "@/lib/format";
import { useHatStatus, type HatStatus } from "@/lib/queries/hat";
import { formatIstDateTime } from "@/lib/time";

type Bot = HatStatus["bots"][number];

const COLUMNS = [{ label: "Bot" }, { label: "Mode" }, { label: "Lifecycle" }, { label: "State" }];

/** The mode the bot runs, why it differs from the one asked for, and any queued change. */
function modeLabel(bot: Bot): string {
  let label = bot.effective_mode;
  if (bot.effective_mode !== bot.execution_mode) {
    label += ` (asked ${bot.execution_mode}: ${bot.mode_reason ?? "?"})`;
  }
  if (bot.pending_mode !== null) {
    const from = bot.pending_from ? `${formatIstDateTime(bot.pending_from)} IST` : "?";
    label += `; ${bot.pending_mode} from ${from}`;
  }
  return label;
}

function BotState({ bot }: Readonly<{ bot: Bot }>) {
  return bot.manage_only ? (
    <Badge tone="warning">manage-only: {bot.reasons.join(", ")}</Badge>
  ) : (
    <Badge tone="profit">active</Badge>
  );
}

function BotDetails({
  bot,
  kills,
  onClose,
}: Readonly<{ bot: Bot; kills: HatStatus["kill_switches"]; onClose: () => void }>) {
  const mine = kills.filter((kill) => kill.bot_id === bot.id);
  return (
    <Drawer title={bot.name} subtitle={`Risk cluster ${bot.risk_cluster}`} onClose={onClose}>
      <DetailList>
        <LabelledValue label="Mode now">
          <span className="text-mode">{bot.effective_mode}</span>
        </LabelledValue>
        <LabelledValue label="Mode asked">{bot.execution_mode}</LabelledValue>
        <LabelledValue label="Why they differ" className="col-span-2">
          {bot.mode_reason ?? PLACEHOLDER}
        </LabelledValue>
        <LabelledValue label="Queued change" className="col-span-2">
          {bot.pending_mode === null
            ? PLACEHOLDER
            : `${bot.pending_mode} from ${bot.pending_from ? formatIstDateTime(bot.pending_from) : "?"} IST`}
        </LabelledValue>
        <LabelledValue label="Status">{bot.status}</LabelledValue>
        <LabelledValue label="Lifecycle">{bot.lifecycle_state}</LabelledValue>
        <LabelledValue label="State" className="col-span-2">
          <BotState bot={bot} />
        </LabelledValue>
        <LabelledValue label="Kill switches" className="col-span-2">
          {mine.length === 0 ? (
            "none recorded"
          ) : (
            <ul className="space-y-1">
              {mine.map((kill) => (
                <li key={kill.channel}>
                  <Badge tone={kill.state === "tripped" ? "loss" : "profit"}>
                    {kill.channel}: {kill.state}
                  </Badge>
                </li>
              ))}
            </ul>
          )}
        </LabelledValue>
      </DetailList>
    </Drawer>
  );
}

function BotRow({ bot, onOpen }: Readonly<{ bot: Bot; onOpen: () => void }>) {
  return (
    <TableRow>
      <TableCell>
        <RowOpener label={`Details of bot ${bot.name}`} onOpen={onOpen}>
          {bot.name}
        </RowOpener>
      </TableCell>
      <TableCell className="text-mode">{modeLabel(bot)}</TableCell>
      <TableCell className="text-foreground-secondary">{bot.lifecycle_state}</TableCell>
      <TableCell>
        <BotState bot={bot} />
      </TableCell>
    </TableRow>
  );
}

/** Every bot with its effective mode, lifecycle and manage-only state; a row opens its details. */
export function BotsPanel({ className }: Readonly<{ className?: string }>) {
  const status = useHatStatus();
  const [openId, setOpenId] = useState<string | null>(null);
  const bots = { ...status, data: status.data?.bots };
  const open = bots.data?.find((bot) => bot.id === openId);
  return (
    <QuerySection
      title="Bots"
      query={bots}
      isEmpty={noRows}
      empty="No bots."
      meta={rowCount}
      className={className}
    >
      {(data) => (
        <>
          <DataTable columns={COLUMNS}>
            {data.map((bot) => (
              <BotRow key={bot.id} bot={bot} onOpen={() => setOpenId(bot.id)} />
            ))}
          </DataTable>
          {open ? (
            <BotDetails
              bot={open}
              kills={status.data?.kill_switches ?? []}
              onClose={() => setOpenId(null)}
            />
          ) : null}
        </>
      )}
    </QuerySection>
  );
}
