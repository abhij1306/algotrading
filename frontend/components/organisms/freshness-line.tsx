import { Badge } from "@/components/atoms/badge";
import { freshness } from "@/lib/queries/freshness";
import { useHatStatus } from "@/lib/queries/hat";
import { formatIstTime } from "@/lib/time";

/** When the backend took the status snapshot (IST), or that the view has stopped refreshing. */
export function FreshnessLine() {
  return (
    <output className="text-sm">
      <FreshnessText />
    </output>
  );
}

function FreshnessText() {
  const status = useHatStatus();
  const asOf = status.data ? `${formatIstTime(status.data.as_of)} IST` : "";
  switch (freshness(status)) {
    case "loading":
      return <span className="text-foreground-muted">Connecting…</span>;
    case "error":
      return <Badge tone="loss">Backend unreachable</Badge>;
    case "stale":
      return <Badge tone="warning">Stale, last snapshot {asOf}</Badge>;
    case "fresh":
      return (
        <span className="text-foreground-muted">
          As of <span className="tabular-nums text-foreground-secondary">{asOf}</span>
        </span>
      );
  }
}
