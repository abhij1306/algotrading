import { Decimal } from "@/components/atoms/number";
import { noRows, QuerySection } from "@/components/molecules/query-section";
import { PLACEHOLDER } from "@/lib/format";
import { useHatWatchlist, useSharedStale, type HatWatchlist } from "@/lib/queries/hat";
import { formatIstDate } from "@/lib/time";

const WATCHLIST_TOP = 10;

function BotWatchlist({ bot }: Readonly<{ bot: HatWatchlist[number] }>) {
  return (
    <div>
      <p className="mb-2 flex items-baseline justify-between gap-4 text-sm">
        <span>{bot.bot}</span>
        <span className="text-xs text-foreground-muted">
          {bot.trade_date ? formatIstDate(bot.trade_date) : PLACEHOLDER}
        </span>
      </p>
      {bot.entries.length === 0 ? (
        <p className="text-sm text-foreground-muted">No candidates.</p>
      ) : (
        <ol className="flex flex-wrap gap-x-5 gap-y-1.5 text-sm">
          {bot.entries.slice(0, WATCHLIST_TOP).map((entry) => (
            <li key={entry.symbol} className="whitespace-nowrap">
              {entry.symbol}{" "}
              <span className="font-mono tabular-nums text-foreground-muted">
                <Decimal value={entry.score} />
              </span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

/** Each bot's top watchlist candidates with their scores. */
export function WatchlistPanel({ className }: Readonly<{ className?: string }>) {
  const watchlist = useHatWatchlist();
  const sharedStale = useSharedStale() !== null;
  return (
    <QuerySection
      sharedStale={sharedStale}
      title="Watchlist"
      query={watchlist}
      isEmpty={noRows}
      empty="No bots."
      meta={() => `top ${WATCHLIST_TOP} per bot`}
      className={className}
    >
      {(data) => (
        <div className="space-y-6">
          {data.map((bot) => (
            <BotWatchlist key={bot.bot_id} bot={bot} />
          ))}
        </div>
      )}
    </QuerySection>
  );
}
