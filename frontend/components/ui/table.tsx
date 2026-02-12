import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Table Component System
 * 
 * High-density data tables optimized for trading terminals.
 * Supports sticky headers, consistent typography, and financial data display.
 */

const Table = React.forwardRef<
  HTMLTableElement,
  React.HTMLAttributes<HTMLTableElement>
>(({ className, ...props }, ref) => (
  <div className="relative w-full overflow-auto">
    <table
      ref={ref}
      className={cn("w-full caption-bottom text-sm", className)}
      {...props}
    />
  </div>
));
Table.displayName = "Table";

const TableHeader = React.forwardRef<
  HTMLTableSectionElement,
  React.HTMLAttributes<HTMLTableSectionElement>
>(({ className, ...props }, ref) => (
  <thead
    ref={ref}
    className={cn(
      "sticky top-0 z-10 bg-[var(--color-base)]",
      "[&_tr]:border-b-0",
      className
    )}
    {...props}
  />
));
TableHeader.displayName = "TableHeader";

const TableBody = React.forwardRef<
  HTMLTableSectionElement,
  React.HTMLAttributes<HTMLTableSectionElement>
>(({ className, ...props }, ref) => (
  <tbody
    ref={ref}
    className={cn("[&_tr]:border-b-0", className)}
    {...props}
  />
));
TableBody.displayName = "TableBody";

const TableFooter = React.forwardRef<
  HTMLTableSectionElement,
  React.HTMLAttributes<HTMLTableSectionElement>
>(({ className, ...props }, ref) => (
  <tfoot
    ref={ref}
    className={cn(
      "border-t border-[var(--border-subtle)] bg-[var(--color-surface)] font-medium",
      className
    )}
    {...props}
  />
));
TableFooter.displayName = "TableFooter";

const TableRow = React.forwardRef<
  HTMLTableRowElement,
  React.HTMLAttributes<HTMLTableRowElement> & {
    variant?: "default" | "ghost" | "active";
  }
>(({ className, variant = "default", ...props }, ref) => {
  const variants = {
    default: "border-b border-[var(--border-subtle)] hover:bg-[var(--glass-highlight)]",
    ghost: "row-ghost hover:bg-[var(--glass-highlight)]",
    active: "bg-[var(--color-primary-bg)] border-[var(--color-primary)]/20",
  };

  return (
    <tr
      ref={ref}
      className={cn(
        "transition-colors h-11",
        variants[variant],
        className
      )}
      {...props}
    />
  );
});
TableRow.displayName = "TableRow";

const TableHead = React.forwardRef<
  HTMLTableCellElement,
  React.ThHTMLAttributes<HTMLTableCellElement> & {
    numeric?: boolean;
    sortable?: boolean;
    sorted?: "asc" | "desc" | null;
  }
>(({ className, numeric, sortable, sorted, children, ...props }, ref) => (
  <th
    ref={ref}
    className={cn(
      "h-10 px-3 text-xs font-medium text-[var(--text-secondary)] uppercase tracking-wide text-left align-middle",
      "[&:has([role=checkbox])]:pr-0",
      numeric && "text-right font-mono tabular-nums",
      sortable && "cursor-pointer select-none hover:text-[var(--text-primary)]",
      className
    )}
    {...props}
  >
    <div className={cn("flex items-center gap-2", numeric && "justify-end")}>
      {children}
      {sortable && sorted && (
        <span className="text-[var(--text-muted)]">
          {sorted === "asc" ? "↑" : "↓"}
        </span>
      )}
    </div>
  </th>
));
TableHead.displayName = "TableHead";

const TableCell = React.forwardRef<
  HTMLTableCellElement,
  React.TdHTMLAttributes<HTMLTableCellElement> & {
    numeric?: boolean;
    profit?: boolean;
    loss?: boolean;
  }
>(({ className, numeric, profit, loss, ...props }, ref) => (
  <td
    ref={ref}
    className={cn(
      "px-3 py-2 align-middle text-sm text-[var(--text-primary)]",
      "[&:has([role=checkbox])]:pr-0",
      numeric && "font-mono tabular-nums text-right",
      profit && "text-[var(--color-profit)]",
      loss && "text-[var(--color-loss)]",
      className
    )}
    {...props}
  />
));
TableCell.displayName = "TableCell";

const TableCaption = React.forwardRef<
  HTMLTableCaptionElement,
  React.HTMLAttributes<HTMLTableCaptionElement>
>(({ className, ...props }, ref) => (
  <caption
    ref={ref}
    className={cn("mt-4 text-sm text-[var(--text-muted)]", className)}
    {...props}
  />
));
TableCaption.displayName = "TableCaption";

// Dense Table Row (for high-density data)
const DenseTableRow = React.forwardRef<
  HTMLTableRowElement,
  React.HTMLAttributes<HTMLTableRowElement>
>(({ className, ...props }, ref) => (
  <TableRow
    ref={ref}
    className={cn("h-8 text-xs", className)}
    {...props}
  />
));
DenseTableRow.displayName = "DenseTableRow";

// Dense Table Cell
const DenseTableCell = React.forwardRef<
  HTMLTableCellElement,
  React.TdHTMLAttributes<HTMLTableCellElement>
>(({ className, ...props }, ref) => (
  <td
    ref={ref}
    className={cn("px-2 py-1 align-middle text-xs", className)}
    {...props}
  />
));
DenseTableCell.displayName = "DenseTableCell";

export {
  Table,
  TableHeader,
  TableBody,
  TableFooter,
  TableHead,
  TableRow,
  TableCell,
  TableCaption,
  DenseTableRow,
  DenseTableCell,
};
