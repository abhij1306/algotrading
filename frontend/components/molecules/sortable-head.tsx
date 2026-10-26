import type { CSSProperties, ReactNode } from "react";
import { ArrowDown, ArrowUp } from "lucide-react";
import { TableHead } from "@/components/atoms/table";
import { cn } from "@/lib/utils";

/** A column header that sorts its table: click to sort, click again to flip the direction. */
export function SortableHead({
  children,
  dir,
  onSort,
  numeric,
  className,
  style,
}: Readonly<{
  children: ReactNode;
  /** This column's direction when the table is sorted by it, else null. */
  dir: "asc" | "desc" | null;
  onSort: () => void;
  numeric?: boolean;
  className?: string;
  style?: CSSProperties;
}>) {
  let ariaSort: "ascending" | "descending" | "none" = "none";
  if (dir === "asc") ariaSort = "ascending";
  else if (dir === "desc") ariaSort = "descending";
  const Arrow = dir === "asc" ? ArrowUp : ArrowDown;
  return (
    <TableHead numeric={numeric} aria-sort={ariaSort} className={className} style={style}>
      <button
        type="button"
        onClick={onSort}
        className={cn(
          "inline-flex items-center gap-1 rounded hover:text-foreground focus-visible:outline-2 focus-visible:outline-accent",
          dir !== null && "text-foreground",
        )}
      >
        {children}
        <Arrow aria-hidden className={cn("size-3", dir === null && "invisible")} />
      </button>
    </TableHead>
  );
}
