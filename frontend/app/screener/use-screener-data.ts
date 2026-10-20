import { useMemo, useState } from "react";
import { useScreenerIndices, useScreenerResults, type ScreenerRow } from "@/lib/queries/screener";
import { debounce } from "@/lib/debounce";
import {
  nextSort,
  resolveSelectedUniverse,
  sortIndicesForUi,
  toSafeString,
  totalPagesFor,
  type SortField,
  type SortState,
} from "@/components/screener/screener-view";

const NO_ROWS: ScreenerRow[] = [];
const SEARCH_DEBOUNCE_MS = 300;

/**
 * What the screener page reads: the universe, search, page size and sort choices, the universe
 * list, and the server-sorted page of results for them. Every filter or sort change returns to
 * page 1.
 */
export function useScreenerData() {
  const [universeChoice, setUniverseChoice] = useState("NIFTY50");
  const [searchQuery, setSearchQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [currentPage, setCurrentPage] = useState(1);
  const [itemsPerPage, setItemsPerPage] = useState(25);
  const [sort, setSort] = useState<SortState>({ field: "symbol", direction: "asc" });

  const debouncedSetQuery = useMemo(
    () =>
      debounce((value: unknown) => {
        setDebouncedQuery(toSafeString(value));
        setCurrentPage(1);
      }, SEARCH_DEBOUNCE_MS),
    [],
  );

  const indicesQuery = useScreenerIndices();
  const indices = useMemo(() => sortIndicesForUi(indicesQuery.data ?? []), [indicesQuery.data]);
  // Until the universe list arrives, query the user's initial choice directly.
  const selectedUniverse = indicesQuery.data
    ? resolveSelectedUniverse(universeChoice, indices)
    : universeChoice;

  const resultsQuery = useScreenerResults({
    universe: selectedUniverse,
    page: currentPage,
    limit: itemsPerPage,
    sortBy: sort.field,
    sortOrder: sort.direction,
    query: debouncedQuery,
  });
  const totalResults = resultsQuery.data?.total ?? 0;
  const totalPages = totalPagesFor(totalResults, itemsPerPage);

  // Clamp the page when the result set shrinks (adjusting state during render, not in an effect).
  if (currentPage > totalPages) {
    setCurrentPage(totalPages);
  }

  return {
    indices,
    isIndicesLoading: indicesQuery.isPending,
    indicesError: indicesQuery.error?.message ?? null,
    selectedUniverse,
    searchQuery,
    itemsPerPage,
    sort,
    currentPage,
    totalPages,
    totalResults,
    results: resultsQuery.data?.rows ?? NO_ROWS,
    isLoading: resultsQuery.isFetching,
    error: resultsQuery.error?.message ?? null,
    search: (value: string) => {
      setSearchQuery(value);
      debouncedSetQuery(value);
    },
    chooseUniverse: (id: string) => {
      setUniverseChoice(id);
      setCurrentPage(1);
    },
    choosePageSize: (size: number) => {
      setItemsPerPage(size);
      setCurrentPage(1);
    },
    sortBy: (field: SortField) => {
      setCurrentPage(1);
      setSort((current) => nextSort(current, field));
    },
    previousPage: () => setCurrentPage((page) => Math.max(1, page - 1)),
    nextPage: () => setCurrentPage((page) => Math.min(totalPages, page + 1)),
  };
}
