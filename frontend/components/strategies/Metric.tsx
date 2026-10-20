/** A labelled number in a small bordered box. */
export function Metric({ label, value }: Readonly<{ label: string; value: string }>) {
  return (
    <div className="rounded-md border border-border bg-background px-3 py-2">
      <div className="text-xs uppercase tracking-wider text-foreground-muted">{label}</div>
      <div className="mt-1 text-sm font-semibold text-foreground">{value}</div>
    </div>
  );
}
