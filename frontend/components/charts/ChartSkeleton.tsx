/**
 * Loading skeleton for chart components
 * Displayed while Recharts library is being loaded
 */
export function ChartSkeleton({ height = 400 }: { height?: number }) {
  return (
    <div
      className="w-full animate-pulse bg-card/50 rounded-lg flex items-center justify-center"
      style={{ height: `${height}px` }}
    >
      <div className="text-muted-foreground text-sm">Loading chart...</div>
    </div>
  );
}
