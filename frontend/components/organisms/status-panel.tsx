import { Badge } from "@/components/atoms/badge";
import { LabelledValue } from "@/components/molecules/labelled-value";
import { QuerySection } from "@/components/molecules/query-section";
import { PLACEHOLDER } from "@/lib/format";
import { useHatStatus, type HatStatus } from "@/lib/queries/hat";
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

function StatusSummary({ status }: Readonly<{ status: HatStatus }>) {
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm md:grid-cols-4">
      <LabelledValue label="Trade date">{formatIstDate(status.trade_date)}</LabelledValue>
      <LabelledValue label="Account kill">
        <Badge variant={status.kill_account ? "loss" : "profit"}>
          {status.kill_account ? "tripped" : "armed"}
        </Badge>
      </LabelledValue>
      <LabelledValue label="Regime">{regimeText(status.regime)}</LabelledValue>
      <LabelledValue label="Feed stale">
        {status.feed_stale_at ? formatIstTime(status.feed_stale_at) : "no"}
      </LabelledValue>
      <LabelledValue label="Broker sessions" wide>
        {listText(status.sessions.map((s) => `${s.account}: ${s.status}`))}
      </LabelledValue>
      <LabelledValue label="Jobs today" wide>
        {listText(status.jobs.map((j) => `${j.job}: ${j.status}`))}
      </LabelledValue>
    </dl>
  );
}

const never = () => false;

/** Trade date, account kill, regime, feed, broker sessions and today's jobs. */
export function StatusPanel() {
  const status = useHatStatus();
  return (
    <QuerySection title="Status" query={status} isEmpty={never} empty="">
      {(data) => <StatusSummary status={data} />}
    </QuerySection>
  );
}
