import type { HTMLAttributes, TdHTMLAttributes, ThHTMLAttributes } from "react";
import { cn } from "@/lib/utils";

/** Dense table parts with hairline row dividers; `numeric` right-aligns in tabular figures. */

export function Table({
  className,
  children,
  ...props
}: Readonly<HTMLAttributes<HTMLTableElement>>) {
  return (
    <div className="-mx-4 overflow-x-auto px-4 md:mx-0 md:px-0">
      <table className={cn("w-full text-sm text-foreground", className)} {...props}>
        {children}
      </table>
    </div>
  );
}

type SectionProps = Readonly<HTMLAttributes<HTMLTableSectionElement>>;

export function TableHeader({ className, ...props }: SectionProps) {
  return <thead className={className} {...props} />;
}

export function TableBody({ className, ...props }: SectionProps) {
  return <tbody className={className} {...props} />;
}

export function TableRow({ className, ...props }: Readonly<HTMLAttributes<HTMLTableRowElement>>) {
  return <tr className={cn("border-b border-line last:border-0", className)} {...props} />;
}

export function TableHead({
  className,
  numeric,
  ...props
}: Readonly<ThHTMLAttributes<HTMLTableCellElement> & { numeric?: boolean }>) {
  return (
    <th
      className={cn(
        "whitespace-nowrap py-2 pr-6 text-left align-bottom text-xs font-normal text-foreground-muted last:pr-0",
        numeric && "text-right",
        className,
      )}
      {...props}
    />
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
        "whitespace-nowrap py-2.5 pr-6 align-middle last:pr-0",
        numeric && "text-right font-mono tabular-nums",
        className,
      )}
      {...props}
    />
  );
}
