/** Badge colour for a backtest run status. */
export function runStatusVariant(status: string): "profit" | "loss" | "warning" {
  if (status === "completed") return "profit";
  return status === "failed" ? "loss" : "warning";
}
