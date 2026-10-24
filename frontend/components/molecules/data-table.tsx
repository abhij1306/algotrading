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
