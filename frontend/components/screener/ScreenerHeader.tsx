import type { ReactNode } from "react";
import { Loader2, Search } from "lucide-react";
import { Badge } from "@/components/ui";
import type { ScreenerIndex } from "@/lib/queries/screener";

const RPP_OPTIONS = [25, 50, 100];

interface ScreenerHeaderProps {
  connected: boolean;
  searchQuery: string;
  onSearch: (value: string) => void;
  indices: readonly ScreenerIndex[];
  isIndicesLoading: boolean;
  selectedUniverse: string;
  onChooseUniverse: (id: string) => void;
  itemsPerPage: number;
  onChoosePageSize: (size: number) => void;
  isLoading: boolean;
  totalResults: number;
}

function resultCount(isIndicesLoading: boolean, isLoading: boolean, total: number): ReactNode {
  if (isIndicesLoading) return "Loading universes...";
  if (isLoading) return <Loader2 className="w-4 h-4 animate-spin inline" />;
  return `${total} stocks`;
}

/** Title and live badge, then the search, universe and page-size filters and the result count. */
export function ScreenerHeader({
  connected,
  searchQuery,
  onSearch,
  indices,
  isIndicesLoading,
  selectedUniverse,
  onChooseUniverse,
  itemsPerPage,
  onChoosePageSize,
  isLoading,
  totalResults,
}: Readonly<ScreenerHeaderProps>) {
  return (
    <header className="flex h-12 shrink-0 items-center justify-between border-b border-border bg-surface px-4">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold text-foreground">Stock Screener</h1>
        {connected && (
          <Badge variant="profit" pulse>
            LIVE
          </Badge>
        )}
      </div>

      <div className="flex items-center gap-3">
        <div className="flex items-center gap-2 px-3 py-1.5 rounded border border-border bg-background">
          <Search className="w-4 h-4 text-foreground-muted" />
          <input
            type="text"
            value={searchQuery}
            onChange={(event) => onSearch(event.target.value)}
            placeholder="Search symbol..."
            className="bg-transparent outline-none w-32 text-sm"
          />
        </div>

        <select
          value={selectedUniverse}
          onChange={(event) => onChooseUniverse(event.target.value)}
          disabled={isIndicesLoading || indices.length === 0}
          className="px-3 py-1.5 rounded text-sm border border-border bg-background outline-none"
        >
          {isIndicesLoading && <option value="">Loading universes...</option>}
          {indices.map((index) => (
            <option key={index.id} value={index.id}>
              {index.name}
            </option>
          ))}
        </select>

        <select
          value={itemsPerPage}
          onChange={(event) => onChoosePageSize(Number(event.target.value))}
          className="px-2 py-1.5 rounded text-sm border border-border bg-background outline-none"
        >
          {RPP_OPTIONS.map((rpp) => (
            <option key={rpp} value={rpp}>
              {rpp}/page
            </option>
          ))}
        </select>

        <span className="text-sm text-foreground-muted">
          {resultCount(isIndicesLoading, isLoading, totalResults)}
        </span>
      </div>
    </header>
  );
}
