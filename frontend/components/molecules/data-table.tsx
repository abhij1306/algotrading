import type { ReactNode } from "react";
import { Table, TableBody, TableHead, TableHeader, TableRow } from "@/components/atoms/table";

export type Column = Readonly<{ label: string; numeric?: boolean }>;

/** A table with one header row from `columns`; `children` are the body rows. */
export function DataTable({
  columns,
  children,
}: Readonly<{ columns: readonly Column[]; children: ReactNode }>) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          {columns.map((column) => (
            <TableHead key={column.label} numeric={column.numeric}>
              {column.label}
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>{children}</TableBody>
    </Table>
  );
}

/** A row's first cell text as a button that opens the row's details. */
export function RowOpener({
  label,
  onOpen,
  children,
}: Readonly<{ label: string; onOpen: () => void; children: ReactNode }>) {
  return (
    <button
      type="button"
      onClick={onOpen}
      aria-label={label}
      className="-mx-1 rounded px-1 text-left hover:text-accent focus-visible:outline-2 focus-visible:outline-accent"
    >
      {children}
    </button>
  );
}
