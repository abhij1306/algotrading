import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Table Component System
 *
 * High-density data tables optimized for trading terminals.
 * Supports sticky headers, consistent typography, and financial data display.
 */

const Table = React.forwardRef<HTMLTableElement, React.HTMLAttributes<HTMLTableElement>>(
  ({ className, children, ...props }, ref) => (
    <div className="relative w-full overflow-auto rounded-md border border-border">
      <table
        ref={ref}
        className={cn("w-full caption-bottom text-sm text-foreground", className)}
        {...props}
      >
        {children}
      </table>
    </div>
  )
);
Table.displayName = "Table";

const TableHeader = React.forwardRef<
  HTMLTableSectionElement,
  React.HTMLAttributes<HTMLTableSectionElement>
>(({ className, ...props }, ref) => (
  <thead
    ref={ref}
    className={cn("sticky top-0 z-10 bg-background-secondary border-b border-border", className)}
    {...props}
  />
));
TableHeader.displayName = "TableHeader";

const TableBody = React.forwardRef<
  HTMLTableSectionElement,
  React.HTMLAttributes<HTMLTableSectionElement>
>(({ className, ...props }, ref) => (
  <tbody ref={ref} className={cn("[&_tr:last-child]:border-0", className)} {...props} />
));
TableBody.displayName = "TableBody";

const TableFooter = React.forwardRef<
  HTMLTableSectionElement,
  React.HTMLAttributes<HTMLTableSectionElement>
>(({ className, ...props }, ref) => (
  <tfoot
    ref={ref}
    className={cn("border-t border-border bg-background-secondary font-medium", className)}
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
    default: "border-b border-border hover:bg-background-secondary/50",
    ghost: "hover:bg-background-secondary/50",
    active: "bg-primary-light border-primary/20",
  };

  return (
    <tr
      ref={ref}
      className={cn("transition-colors h-10", variants[variant], className)}
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
      "h-9 px-4 text-left align-middle font-medium text-foreground-muted text-xs uppercase tracking-wider",
      numeric && "text-right font-mono",
      sortable && "cursor-pointer select-none hover:text-foreground transition-colors",
      className
    )}
    {...props}
  >
    <div className={cn("flex items-center gap-1", numeric && "justify-end")}>
      {children}
      {sorted && <span className="text-primary">{sorted === "asc" ? "↑" : "↓"}</span>}
    </div>
  </th>
));
TableHead.displayName = "TableHead";

const TableCell = React.forwardRef<
  HTMLTableCellElement,
  React.TdHTMLAttributes<HTMLTableCellElement> & {
    numeric?: boolean;
    variant?: "default" | "profit" | "loss" | "muted";
  }
>(({ className, numeric, variant = "default", ...props }, ref) => {
  const variantStyles = {
    default: "text-foreground",
    profit: "text-profit font-medium",
    loss: "text-loss font-medium",
    muted: "text-foreground-muted",
  };

  return (
    <td
      ref={ref}
      className={cn(
        "px-4 py-2 align-middle text-sm",
        numeric && "text-right font-mono tabular-nums",
        variantStyles[variant],
        className
      )}
      {...props}
    />
  );
});
TableCell.displayName = "TableCell";

const TableCaption = React.forwardRef<
  HTMLTableCaptionElement,
  React.HTMLAttributes<HTMLTableCaptionElement>
>(({ className, ...props }, ref) => (
  <caption ref={ref} className={cn("mt-4 text-sm text-foreground-muted", className)} {...props} />
));
TableCaption.displayName = "TableCaption";

// Dense Table Row (for high-density data)
const DenseTableRow = React.forwardRef<
  HTMLTableRowElement,
  React.HTMLAttributes<HTMLTableRowElement>
>(({ className, ...props }, ref) => (
  <TableRow ref={ref} className={cn("h-8 text-xs", className)} {...props} />
));
DenseTableRow.displayName = "DenseTableRow";

/**
 * Dense Table Cell
 */
const DenseTableCell = React.forwardRef<
  HTMLTableCellElement,
  React.TdHTMLAttributes<HTMLTableCellElement> & { numeric?: boolean }
>(({ className, ...props }, ref) => (
  <TableCell ref={ref} className={cn("py-1", className)} {...props} />
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
