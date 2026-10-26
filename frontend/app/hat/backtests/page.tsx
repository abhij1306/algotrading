import { useState } from "react";
import { Button } from "@/components/atoms/button";
import { JobsPanel } from "@/components/organisms/backtests/jobs-panel";
import { RunsView } from "@/components/organisms/backtests/runs-view";
import { StartForm } from "@/components/organisms/backtests/start-form";
import { PageLayout } from "@/components/templates/page-layout";

/** The backtest run folders: read and compare them, and start a research run or a small sweep. */
export default function BacktestsPage() {
  const [starting, setStarting] = useState(false);
  return (
    <PageLayout
      title="Backtests"
      description="Research runs only: a promotion run starts from the command line."
      aside={
        starting ? null : (
          <Button variant="primary" onClick={() => setStarting(true)}>
            New run or sweep
          </Button>
        )
      }
    >
      <div className="space-y-14">
        {starting ? (
          <section aria-label="New run or sweep">
            <h2 className="mb-4 text-lg">New run or sweep</h2>
            <StartForm onDone={() => setStarting(false)} />
          </section>
        ) : null}
        <JobsPanel />
        <RunsView />
      </div>
    </PageLayout>
  );
}
