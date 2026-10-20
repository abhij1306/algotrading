import { useCallback } from "react";
import { useNavigate } from "react-router";
import { ScreenerHeader } from "@/components/screener/ScreenerHeader";
import { ScreenerPagination } from "@/components/screener/ScreenerPagination";
import { ScreenerTable } from "@/components/screener/ScreenerTable";
import { getEmptyStateMessage } from "@/components/screener/screener-view";
import { useLiveOverlay } from "./use-live-overlay";
import { useScreenerData } from "./use-screener-data";

export default function ScreenerPage() {
  const navigate = useNavigate();
  const data = useScreenerData();
  const { connected, quotesFor } = useLiveOverlay(data.results);

  const openTerminal = useCallback(
    (symbol: string) => void navigate(`/terminal?symbol=${encodeURIComponent(symbol)}`),
    [navigate],
  );

  return (
    <div className="flex flex-col h-screen">
      <ScreenerHeader
        connected={connected}
        searchQuery={data.searchQuery}
        onSearch={data.search}
        indices={data.indices}
        isIndicesLoading={data.isIndicesLoading}
        selectedUniverse={data.selectedUniverse}
        onChooseUniverse={data.chooseUniverse}
        itemsPerPage={data.itemsPerPage}
        onChoosePageSize={data.choosePageSize}
        isLoading={data.isLoading}
        totalResults={data.totalResults}
      />

      <div className="flex-1 overflow-auto">
        {data.indicesError && (
          <div className="px-4 py-2 text-sm text-loss border-b border-border">
            {data.indicesError}
          </div>
        )}
        {data.error && (
          <div className="px-4 py-2 text-sm text-loss border-b border-border">{data.error}</div>
        )}

        <ScreenerTable
          rows={data.results}
          sort={data.sort}
          onSort={data.sortBy}
          quotesFor={quotesFor}
          onOpen={openTerminal}
        />

        {data.results.length === 0 && !data.isLoading && (
          <div className="p-8 text-center text-foreground-muted">
            {getEmptyStateMessage(data.indices, data.error)}
          </div>
        )}
      </div>

      {data.totalPages > 1 && (
        <ScreenerPagination
          currentPage={data.currentPage}
          totalPages={data.totalPages}
          itemsPerPage={data.itemsPerPage}
          totalResults={data.totalResults}
          onPrevious={data.previousPage}
          onNext={data.nextPage}
        />
      )}
    </div>
  );
}
