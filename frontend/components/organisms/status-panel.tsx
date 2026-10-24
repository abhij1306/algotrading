import { Badge } from "@/components/atoms/badge";
import { LabelledValue } from "@/components/molecules/labelled-value";
import { QuerySection } from "@/components/molecules/query-section";
import { PLACEHOLDER } from "@/lib/format";
import { useHatStatus, useSharedStale, type HatStatus } from "@/lib/queries/hat";
import { formatIstDate, formatIstTime } from "@/lib/time";

function regimeText(regime: HatStatus["regime"]): string {
  if (!regime) return PLACEHOLDER;
  const confirmed = regime.confirmed ? "" : " (unconfirmed)";
  return `${regime.state}${confirmed} · ${formatIstDate(regime.trade_date)}`;
}

/** "a: x, b: y", or the placeholder for none. */
function listText(items: readonly string[]): string {
  return items.length === 0 ? PLACEHOLDER : items.join(", ");
}

/** The tripped kill switches below account level, named by bot and channel. */
function trippedKills(status: HatStatus): string[] {
  const names = new Map(status.bots.map((bot) => [bot.id, bot.name]));
  return status.kill_switches
    .filter((kill) => kill.state === "tripped" && kill.scope !== "account")
    .map((kill) => {
      const who = kill.bot_id === null ? kill.scope : (names.get(kill.bot_id) ?? kill.scope);
      return `${who} (${kill.channel})`;
    });
}

function StatusSummary({ status }: Readonly<{ status: HatStatus }>) {
  const kills = trippedKills(status);
  return (
    <dl className="grid grid-cols-2 gap-x-8 gap-y-6 md:grid-cols-4">
      <LabelledValue label="Account kill">
        <Badge tone={status.kill_account ? "loss" : "profit"}>
          {status.kill_account ? "tripped" : "armed"}
        </Badge>
      </LabelledValue>
      <LabelledValue label="Trade date">{formatIstDate(status.trade_date)}</LabelledValue>
      <LabelledValue label="Regime">{regimeText(status.regime)}</LabelledValue>
      <LabelledValue label="Market feed">
        {status.feed_stale_at ? (
          <Badge tone="warning">stale since {formatIstTime(status.feed_stale_at)} IST</Badge>
        ) : (
          <Badge tone="profit">live</Badge>
        )}
      </LabelledValue>
      <LabelledValue label="Bot kills tripped" className="col-span-2">
        {kills.length === 0 ? "none" : <span className="text-loss">{kills.join(", ")}</span>}
      </LabelledValue>
      <LabelledValue label="Broker sessions" className="col-span-2">
        {listText(status.sessions.map((s) => `${s.account}: ${s.status}`))}
      </LabelledValue>
      <LabelledValue label="Jobs today" className="col-span-2 md:col-span-4">
        {listText(status.jobs.map((j) => `${j.job}: ${j.status}`))}
      </LabelledValue>
    </dl>
  );
}

const never = () => false;

/** Account kill, trade date, regime, feed, bot kills, broker sessions and today's jobs. */
export function StatusPanel({ className }: Readonly<{ className?: string }>) {
  const status = useHatStatus();
  const sharedStale = useSharedStale() !== null;
  return (
    <QuerySection
      sharedStale={sharedStale}
      title="Status"
      query={status}
      isEmpty={never}
      empty=""
      className={className}
    >
      {(data) => <StatusSummary status={data} />}
    </QuerySection>
  );
}
