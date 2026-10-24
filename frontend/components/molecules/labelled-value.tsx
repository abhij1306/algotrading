import type { ReactNode } from "react";

/** One label over its value inside a `<dl>`. */
export function LabelledValue({
  label,
  className,
  children,
}: Readonly<{ label: string; className?: string; children: ReactNode }>) {
  return (
    <div className={className}>
      <dt className="text-xs text-foreground-muted">{label}</dt>
      <dd className="mt-1">{children}</dd>
    </div>
  );
}
