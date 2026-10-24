import type { HTMLAttributes, TdHTMLAttributes, ThHTMLAttributes } from "react";
import { cn } from "@/lib/utils";

/** High-density table parts; `numeric` right-aligns a column in tabular figures. */

export function Table({
  className,
  children,
  ...props
}: Readonly<HTMLAttributes<HTMLTableElement>>) {
  return (
    <div className="relative w-full overflow-auto rounded-md border border-border">
      <table className={cn("w-full caption-bottom text-sm text-foreground", className)} {...props}>
        {children}
      </table>
    </div>
  );
}

type SectionProps = Readonly<HTMLAttributes<HTMLTableSectionElement>>;

export function TableHeader({ className, ...props }: SectionProps) {
  return (
    <thead
      className={cn("sticky top-0 z-10 bg-background-secondary border-b border-border", className)}
      {...props}
    />
  );
}

export function TableBody({ className, ...props }: SectionProps) {
  return <tbody className={cn("[&_tr:last-child]:border-0", className)} {...props} />;
}

export function TableRow({ className, ...props }: Readonly<HTMLAttributes<HTMLTableRowElement>>) {
  return (
    <tr
      className={cn(
        "transition-colors h-10",
        "border-b border-border hover:bg-background-secondary/50",
        className,
      )}
      {...props}
    />
  );
}

export function TableHead({
  className,
  numeric,
  children,
  ...props
}: Readonly<ThHTMLAttributes<HTMLTableCellElement> & { numeric?: boolean }>) {
  return (
    <th
      className={cn(
        "h-9 px-4 text-left align-middle font-medium text-foreground-muted text-xs uppercase tracking-wider",
        numeric && "text-right font-mono",
        className,
      )}
      {...props}
    >
      <div className={cn("flex items-center gap-1", numeric && "justify-end")}>{children}</div>
    </th>
  );
}

export function TableCell({
  className,
  numeric,
  ...props
}: Readonly<TdHTMLAttributes<HTMLTableCellElement> & { numeric?: boolean }>) {
  return (
    <td
      className={cn(
        "px-4 py-2 align-middle text-sm",
        numeric && "text-right font-mono tabular-nums",
        "text-foreground",
        className,
      )}
      {...props}
    />
  );
}
