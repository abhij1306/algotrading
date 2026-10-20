import { Badge, Button } from "@/components/ui";

interface ResultHeaderProps {
  runId: string | undefined;
  /** Both buttons go back to the backtest form. */
  onBack: () => void;
}

/** Breadcrumb with the run id, and the way back to the form. */
export function ResultHeader({ runId, onBack }: Readonly<ResultHeaderProps>) {
  return (
    <div className="flex h-12 shrink-0 items-center justify-between border-b border-border bg-surface px-4">
      <div className="flex items-center gap-2 text-sm">
        <span className="text-foreground-secondary">Backtest</span>
        <span className="text-foreground-muted">/</span>
        <span className="font-semibold text-foreground">Results</span>
        <Badge variant="outline" className="ml-2">
          {runId}
        </Badge>
      </div>
      <div className="flex items-center gap-2">
        <Button variant="outline" size="sm" onClick={onBack}>
          ← Back
        </Button>
        <Button variant="primary" size="sm" onClick={onBack}>
          + New Run
        </Button>
      </div>
    </div>
  );
}
