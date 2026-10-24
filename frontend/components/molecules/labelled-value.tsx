import type { ReactNode } from "react";

/** One label and its value in a description list; `wide` spans the rest of the row. */
export function LabelledValue({
  label,
  wide = false,
  children,
}: Readonly<{ label: string; wide?: boolean; children: ReactNode }>) {
  return (
    <>
      <dt className="text-foreground-muted">{label}</dt>
      <dd className={wide ? "col-span-1 md:col-span-3" : undefined}>{children}</dd>
    </>
  );
}
