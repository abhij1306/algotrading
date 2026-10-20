import { useCallback, useMemo, useState } from "react";
import { debounce } from "@/lib/debounce";
import { useSymbolSearch } from "@/lib/queries/terminal";
import { searchView } from "../lib/terminal-view";

/**
 * Debounced symbol search; results show once at least two characters are typed, and only the
 * results for exactly the typed text, so one from an earlier query can never be picked.
 */
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
  const symbolSearchQuery = useSymbolSearch(searchTerm);
  const { active, results, loading } = searchView({
    text,
    searchTerm,
    data: symbolSearchQuery.data,
    isPlaceholderData: symbolSearchQuery.isPlaceholderData,
  });
  return {
    text,
    active,
    results,
    loading,
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
