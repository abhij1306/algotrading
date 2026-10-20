import { Plus, Search } from "lucide-react";
import { useAddToWatchlist, type SearchResultItem } from "@/lib/queries/terminal";
import { useWatchlistSearch } from "../hooks/use-watchlist-search";
import { errorText, watchlistInstrumentType } from "../lib/terminal-view";

function WatchlistSearchResults({
  results,
  loading,
  onPick,
}: Readonly<{
  results: SearchResultItem[];
  loading: boolean;
  onPick: (item: SearchResultItem) => void;
}>) {
  if (loading) {
    return <div className="px-2 py-2 text-xs text-foreground-muted">Searching...</div>;
  }
  if (results.length === 0) {
    return <div className="px-2 py-2 text-xs text-foreground-muted">No matches</div>;
  }
  return results.map((item) => (
    <button
      key={`${item.symbol}-${item.type || "EQ"}`}
      type="button"
      className="w-full text-left px-2 py-1.5 hover:bg-background-secondary border-b border-border last:border-b-0"
      onClick={() => onPick(item)}
    >
      <div className="text-sm font-medium">{item.symbol}</div>
      <div className="text-xs text-foreground-muted truncate">{item.name}</div>
    </button>
  ));
}

/** Search box that adds a symbol to the watchlist and selects it. */
export function WatchlistSearch({ onAdded }: Readonly<{ onAdded: (symbol: string) => void }>) {
  const { cancelOnUnmount, ...search } = useWatchlistSearch();
  const addToWatchlist = useAddToWatchlist();
  const firstResult = search.results.at(0);

  const addSymbol = (item: SearchResultItem) => {
    const symbol = item.symbol.trim().toUpperCase();
    if (!symbol) return;
    search.cancelPending();
    addToWatchlist.mutate(
      { symbol, name: item.name, instrumentType: watchlistInstrumentType(item) },
      {
        onSuccess: () => {
          onAdded(symbol);
          search.reset();
        },
      },
    );
  };

  return (
    <div className="p-2 border-b border-border">
      <div className="flex items-center gap-2 px-3 py-1.5 rounded bg-background border border-border">
        <Search className="w-4 h-4 text-foreground-muted" />
        <input
          ref={cancelOnUnmount}
          type="text"
          value={search.text}
          onChange={(event) => search.onTextChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && firstResult) {
              event.preventDefault();
              addSymbol(firstResult);
            }
          }}
          placeholder="Search..."
          className="flex-1 bg-transparent outline-none text-sm"
        />
        <button
          type="button"
          className="text-foreground-muted hover:text-foreground disabled:opacity-50"
          disabled={!firstResult}
          onClick={() => {
            if (firstResult) addSymbol(firstResult);
          }}
          aria-label="Add symbol to watchlist"
        >
          <Plus className="w-4 h-4" />
        </button>
      </div>
      {addToWatchlist.error && (
        <div className="mt-1 text-xs text-loss">
          {errorText(addToWatchlist.error, "Failed to add symbol")}
        </div>
      )}
      {search.active && (
        <div className="mt-1 rounded border border-border bg-surface max-h-44 overflow-y-auto">
          <WatchlistSearchResults
            results={search.results}
            loading={search.loading}
            onPick={addSymbol}
          />
        </div>
      )}
    </div>
  );
}
