import { Button } from "@/components/ui";
import { pageRange } from "./screener-view";

interface ScreenerPaginationProps {
  currentPage: number;
  totalPages: number;
  itemsPerPage: number;
  totalResults: number;
  onPrevious: () => void;
  onNext: () => void;
}

/** The shown row range and previous/next paging. */
export function ScreenerPagination({
  currentPage,
  totalPages,
  itemsPerPage,
  totalResults,
  onPrevious,
  onNext,
}: Readonly<ScreenerPaginationProps>) {
  const { from, to } = pageRange(currentPage, itemsPerPage, totalResults);
  return (
    <div className="flex items-center justify-between px-4 py-2 border-t border-border bg-surface">
      <span className="text-sm text-foreground-muted">
        Showing {from}-{to} of {totalResults}
      </span>
      <div className="flex items-center gap-2">
        <Button onClick={onPrevious} disabled={currentPage === 1} variant="secondary" size="sm">
          Previous
        </Button>
        <span className="text-sm px-2">
          Page {currentPage} of {totalPages}
        </span>
        <Button
          onClick={onNext}
          disabled={currentPage === totalPages}
          variant="secondary"
          size="sm"
        >
          Next
        </Button>
      </div>
    </div>
  );
}
