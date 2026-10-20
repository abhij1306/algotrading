import { useCallback, useMemo, useState } from "react";
import { debounce } from "@/lib/debounce";
import { useSymbolSearch, type SearchResultItem } from "@/lib/queries/terminal";

const NO_SEARCH_RESULTS: SearchResultItem[] = [];

/** Debounced symbol search; results show once at least two characters are typed. */
export function useWatchlistSearch() {
  const [text, setText] = useState("");
  const [searchTerm, setSearchTerm] = useState("");
  const debouncedSetSearchTerm = useMemo(
    () => debounce((value: unknown) => setSearchTerm(typeof value === "string" ? value : ""), 250),
    [],
  );
  // Stable ref callback on the search input: its cleanup drops a pending search on unmount.
  const cancelOnUnmount = useCallback(
    () => () => debouncedSetSearchTerm.cancel(),
    [debouncedSetSearchTerm],
  );
  const active = text.trim().length >= 2;
  const symbolSearchQuery = useSymbolSearch(searchTerm);
  const results = active ? (symbolSearchQuery.data ?? NO_SEARCH_RESULTS) : NO_SEARCH_RESULTS;
  return {
    text,
    active,
    results,
    loading: symbolSearchQuery.isFetching,
    cancelOnUnmount,
    onTextChange: (value: string) => {
      setText(value);
      debouncedSetSearchTerm(value.trim());
    },
    /** Drop any pending debounced search so a stale query does not fire. */
    cancelPending: () => debouncedSetSearchTerm.cancel(),
    reset: () => {
      setText("");
      setSearchTerm("");
    },
  };
}
