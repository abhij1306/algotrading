import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Table Component System
 * 
 * High-density data tables optimized for trading terminals.
 * Supports dense rows, sorting, and financial data display.
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
      "[&_tr]:border-b border-[var(--border-default)]",
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
    className={cn("[&_tr:last-child]:border-0", className)}
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
      "border-t border-[var(--border-default)] bg-[var(--color-elevated)] font-medium [&>tr]:last:border-b-0",
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
    ghost: "row-ghost",
    active: "bg-[var(--color-primary-bg)] border-[var(--color-primary)]/20",
  };

  return (
    <tr
      ref={ref}
      className={cn(
        "transition-colors",
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
    sortable?: boolean;
    sorted?: "asc" | "desc" | null;
  }
>(({ className, sortable, sorted, children, ...props }, ref) => (
  <th
    ref={ref}
    className={cn(
      "h-10 px-4 text-left align-middle font-medium text-[var(--text-secondary)] [&:has([role=checkbox])]:pr-0",
      sortable && "cursor-pointer select-none hover:text-[var(--text-primary)]",
      className
    )}
    {...props}
  >
    <div className="flex items-center gap-2">
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
      "p-4 align-middle [&:has([role=checkbox])]:pr-0",
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
