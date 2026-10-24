import type { ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { PageContainer } from "@/components/ui/PageContainer";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { formatNumber, PLACEHOLDER } from "@/lib/format";
import {
  useHatAllocations,
  useHatEvents,
  useHatPositions,
  useHatSignals,
  useHatStatus,
  useHatWatchlist,
  type HatAllocations,
  type HatEvents,
  type HatPositions,
  type HatSignals,
  type HatStatus,
  type HatWatchlist,
} from "@/lib/queries/hat";
import { formatIstDate, formatIstDateTime, formatIstTime } from "@/lib/time";

const WATCHLIST_TOP = 10;

/** A decimal string from the API, shown with Indian grouping. */
function decimalText(value: string | null | undefined, decimals: number = 2): string {
  return value == null ? PLACEHOLDER : formatNumber(Number(value), decimals);
}

/** A ratio string (0.25) as a percent ("25.00%"). */
function percentText(value: string): string {
  return `${formatNumber(Number(value) * 100, 2)}%`;
}

function regimeText(regime: HatStatus["regime"]): string {
  if (!regime) return PLACEHOLDER;
  const confirmed = regime.confirmed ? "" : " (unconfirmed)";
  return `${regime.state}${confirmed} · ${formatIstDate(regime.trade_date)}`;
}

type Query<T> = { data: T | undefined; error: Error | null; isPending: boolean };

/** Loading, error and empty states shared by every section; `children` renders the data. */
function Section<T>(
  props: Readonly<{
    title: string;
    query: Query<T>;
    isEmpty: (data: T) => boolean;
    empty: string;
    children: (data: T) => ReactNode;
  }>,
) {
  const { title, query, isEmpty, empty, children } = props;
  let body: ReactNode;
  if (query.data !== undefined) {
    body = isEmpty(query.data) ? (
      <p className="text-sm text-foreground-muted">{empty}</p>
    ) : (
      children(query.data)
    );
  } else if (query.error) {
    body = (
      <p role="alert" className="text-sm text-loss">
        {query.error.message}
      </p>
    );
  } else {
    body = <p className="text-sm text-foreground-muted">Loading…</p>;
  }
  return (
    <Card aria-label={title} role="region">
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent>{body}</CardContent>
    </Card>
  );
}

function StatusSummary({ status }: Readonly<{ status: HatStatus }>) {
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm md:grid-cols-4">
      <dt className="text-foreground-muted">Trade date</dt>
      <dd>{formatIstDate(status.trade_date)}</dd>
      <dt className="text-foreground-muted">Account kill</dt>
      <dd>
        <Badge variant={status.kill_account ? "loss" : "profit"}>
          {status.kill_account ? "tripped" : "armed"}
        </Badge>
      </dd>
      <dt className="text-foreground-muted">Regime</dt>
      <dd>{regimeText(status.regime)}</dd>
      <dt className="text-foreground-muted">Feed stale</dt>
      <dd>{status.feed_stale_at ? formatIstTime(status.feed_stale_at) : "no"}</dd>
      <dt className="text-foreground-muted">Broker sessions</dt>
      <dd className="col-span-1 md:col-span-3">
        {status.sessions.length === 0
          ? PLACEHOLDER
          : status.sessions.map((s) => `${s.account}: ${s.status}`).join(", ")}
      </dd>
      <dt className="text-foreground-muted">Jobs today</dt>
      <dd className="col-span-1 md:col-span-3">
        {status.jobs.length === 0
          ? PLACEHOLDER
          : status.jobs.map((j) => `${j.job}: ${j.status}`).join(", ")}
      </dd>
    </dl>
  );
}

type Bot = HatStatus["bots"][number];

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

function BotsTable({ bots }: Readonly<{ bots: HatStatus["bots"] }>) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Bot</TableHead>
          <TableHead>Mode</TableHead>
          <TableHead>Lifecycle</TableHead>
          <TableHead>State</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {bots.map((bot) => (
          <TableRow key={bot.id}>
            <TableCell>{bot.name}</TableCell>
            <TableCell>{modeLabel(bot)}</TableCell>
            <TableCell>{bot.lifecycle_state}</TableCell>
            <TableCell>
              <Badge variant={bot.manage_only ? "warning" : "profit"}>
                {bot.manage_only ? `manage-only: ${bot.reasons.join(", ")}` : "active"}
              </Badge>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

function AllocationsTable({ allocations }: Readonly<{ allocations: HatAllocations }>) {
  return (
    <>
      <p className="mb-2 text-xs text-foreground-muted">
        Allocation of {allocations.trade_date ? formatIstDate(allocations.trade_date) : PLACEHOLDER}
      </p>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Bot</TableHead>
            <TableHead numeric>Weight</TableHead>
            <TableHead numeric>Regime ×</TableHead>
            <TableHead numeric>Health ×</TableHead>
            <TableHead numeric>Capital</TableHead>
            <TableHead numeric>Risk / trade</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {allocations.rows.map((row) => (
            <TableRow key={row.bot_id}>
              <TableCell>{row.bot}</TableCell>
              <TableCell numeric>{percentText(row.final_weight)}</TableCell>
              <TableCell numeric>{decimalText(row.regime_mult)}</TableCell>
              <TableCell numeric>{decimalText(row.health_mult)}</TableCell>
              <TableCell numeric>{decimalText(row.capital)}</TableCell>
              <TableCell numeric>{percentText(row.risk_pct)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </>
  );
}

function WatchlistTables({ watchlist }: Readonly<{ watchlist: HatWatchlist }>) {
  return (
    <div className="space-y-4">
      {watchlist.map((bot) => (
        <div key={bot.bot_id}>
          <p className="mb-1 text-sm font-medium">
            {bot.bot} · {bot.trade_date ? formatIstDate(bot.trade_date) : PLACEHOLDER}
          </p>
          {bot.entries.length === 0 ? (
            <p className="text-sm text-foreground-muted">No candidates.</p>
          ) : (
            <ol className="flex flex-wrap gap-2 text-sm">
              {bot.entries.slice(0, WATCHLIST_TOP).map((entry) => (
                <li key={entry.symbol}>
                  <Badge variant="outline">
                    {entry.symbol} {decimalText(entry.score)}
                  </Badge>
                </li>
              ))}
            </ol>
          )}
        </div>
      ))}
    </div>
  );
}

function PositionsTable({ positions }: Readonly<{ positions: HatPositions }>) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Bot</TableHead>
          <TableHead>Symbol</TableHead>
          <TableHead numeric>Qty</TableHead>
          <TableHead numeric>Entry</TableHead>
          <TableHead numeric>Stop</TableHead>
          <TableHead>Opened</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {positions.map((p) => (
          <TableRow key={p.id}>
            <TableCell>{p.bot}</TableCell>
            <TableCell>
              {p.symbol}
              {p.is_virtual ? (
                <Badge variant="muted" className="ml-2">
                  virtual
                </Badge>
              ) : null}
            </TableCell>
            <TableCell numeric>{p.qty_open}</TableCell>
            <TableCell numeric>{decimalText(p.avg_entry)}</TableCell>
            <TableCell numeric>{decimalText(p.current_stop)}</TableCell>
            <TableCell>{formatIstDateTime(p.opened_at)}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

type Signal = HatSignals[number];

const CONFIDENCE_VARIANT = { high: "profit", medium: "warning", low: "muted" } as const;

/** Why the signal got its label: the reason code, and the backtest trades of its bucket. */
function basisText(signal: Signal): string {
  const { reason, trades } = signal.confidence_basis;
  const why = typeof reason === "string" ? reason : PLACEHOLDER;
  return typeof trades === "number" ? `${why} · ${trades} trades` : why;
}

function ConfidenceBadge({ signal }: Readonly<{ signal: Signal }>) {
  const label = signal.confidence;
  if (label == null) return <span className="text-foreground-muted">{PLACEHOLDER}</span>;
  const variant =
    label in CONFIDENCE_VARIANT
      ? CONFIDENCE_VARIANT[label as keyof typeof CONFIDENCE_VARIANT]
      : "outline";
  return <Badge variant={variant}>{label}</Badge>;
}

function SignalsTable({ signals }: Readonly<{ signals: HatSignals }>) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Triggered</TableHead>
          <TableHead>Bot</TableHead>
          <TableHead>Symbol</TableHead>
          <TableHead numeric>Trigger</TableHead>
          <TableHead numeric>Stop</TableHead>
          <TableHead numeric>Qty</TableHead>
          <TableHead>Mode</TableHead>
          <TableHead>State</TableHead>
          <TableHead>Confidence</TableHead>
          <TableHead>Basis</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {signals.map((s) => (
          <TableRow key={s.id}>
            <TableCell>{formatIstDateTime(s.triggered_at)}</TableCell>
            <TableCell>{s.bot}</TableCell>
            <TableCell>{s.symbol}</TableCell>
            <TableCell numeric>{decimalText(s.trigger_price)}</TableCell>
            <TableCell numeric>{decimalText(s.stop_price)}</TableCell>
            <TableCell numeric>{s.qty}</TableCell>
            <TableCell>{s.mode}</TableCell>
            <TableCell>{s.state}</TableCell>
            <TableCell>
              <ConfidenceBadge signal={s} />
            </TableCell>
            <TableCell className="text-foreground-muted">{basisText(s)}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

function EventsList({ events }: Readonly<{ events: HatEvents }>) {
  return (
    <ul className="space-y-1 text-sm">
      {events.map((event) => (
        <li key={event.id} className="flex gap-3">
          <span className="font-mono text-foreground-muted">
            {formatIstDateTime(event.occurred_at)}
          </span>
          <span>{event.event_type}</span>
          <span className="text-foreground-muted">{event.actor}</span>
        </li>
      ))}
    </ul>
  );
}

const always = () => false;
const noRows = (rows: readonly unknown[]) => rows.length === 0;

export default function HatPage() {
  const status = useHatStatus();
  const allocations = useHatAllocations();
  const watchlist = useHatWatchlist();
  const positions = useHatPositions();
  const signals = useHatSignals();
  const events = useHatEvents();
  const bots = { ...status, data: status.data?.bots };

  return (
    <PageContainer>
      <h1 className="text-xl font-semibold text-foreground">Hire a Trader</h1>
      <p className="mt-1 mb-4 text-sm text-foreground-secondary">
        Read-only. Kill, re-arm, mode and flatten are Telegram commands.
      </p>
      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Status" query={status} isEmpty={always} empty="">
          {(data) => <StatusSummary status={data} />}
        </Section>
        <Section title="Bots" query={bots} isEmpty={noRows} empty="No bots.">
          {(data) => <BotsTable bots={data} />}
        </Section>
        <Section
          title="Allocations"
          query={allocations}
          isEmpty={(data) => data.rows.length === 0}
          empty="No allocation yet."
        >
          {(data) => <AllocationsTable allocations={data} />}
        </Section>
        <Section title="Watchlist" query={watchlist} isEmpty={noRows} empty="No bots.">
          {(data) => <WatchlistTables watchlist={data} />}
        </Section>
        <Section
          title="Open positions"
          query={positions}
          isEmpty={noRows}
          empty="No open positions."
        >
          {(data) => <PositionsTable positions={data} />}
        </Section>
        <Section title="Signals" query={signals} isEmpty={noRows} empty="No signals.">
          {(data) => <SignalsTable signals={data} />}
        </Section>
        <Section title="Recent events" query={events} isEmpty={noRows} empty="No events.">
          {(data) => <EventsList events={data} />}
        </Section>
      </div>
    </PageContainer>
  );
}
