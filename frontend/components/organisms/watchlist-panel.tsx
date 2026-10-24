import { Badge } from "@/components/atoms/badge";
import { Decimal } from "@/components/atoms/number";
import { Text } from "@/components/atoms/text";
import { noRows, QuerySection } from "@/components/molecules/query-section";
import { PLACEHOLDER } from "@/lib/format";
import { useHatWatchlist, type HatWatchlist } from "@/lib/queries/hat";
import { formatIstDate } from "@/lib/time";

const WATCHLIST_TOP = 10;

function BotWatchlist({ bot }: Readonly<{ bot: HatWatchlist[number] }>) {
  return (
    <div>
      <Text className="mb-1 font-medium">
        {bot.bot} · {bot.trade_date ? formatIstDate(bot.trade_date) : PLACEHOLDER}
      </Text>
      {bot.entries.length === 0 ? (
        <Text tone="muted">No candidates.</Text>
      ) : (
        <ol className="flex flex-wrap gap-2 text-sm">
          {bot.entries.slice(0, WATCHLIST_TOP).map((entry) => (
            <li key={entry.symbol}>
              <Badge variant="outline">
                {entry.symbol} <Decimal value={entry.score} />
              </Badge>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

/** Each bot's top watchlist candidates with their scores. */
export function WatchlistPanel() {
  const watchlist = useHatWatchlist();
  return (
    <QuerySection title="Watchlist" query={watchlist} isEmpty={noRows} empty="No bots.">
      {(data) => (
        <div className="space-y-4">
          {data.map((bot) => (
            <BotWatchlist key={bot.bot_id} bot={bot} />
          ))}
        </div>
      )}
    </QuerySection>
  );
}
