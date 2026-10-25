import { Link } from "react-router";
import { Badge, type Tone } from "@/components/atoms/badge";
import { useBacktestJobs, type BacktestJob } from "@/lib/queries/backtests";
import { formatIstDateTime } from "@/lib/time";

const TONE: Record<string, Tone> = {
  running: "mode",
  completed: "profit",
  failed: "loss",
  refused: "warning",
};

function Progress({ job }: Readonly<{ job: BacktestJob }>) {
  const done = job.outcomes.length;
  return (
    <div className="flex items-center gap-3">
      <progress
        max={job.total}
        value={done}
        aria-label={`${done} of ${job.total} variants finished`}
        className="h-1.5 w-32 overflow-hidden rounded-full accent-accent"
      />
      <span className="text-xs text-foreground-muted">
        {done} of {job.total} {job.sweep ? "variants" : "run"} finished
      </span>
    </div>
  );
}

function JobItem({ job }: Readonly<{ job: BacktestJob }>) {
  return (
    <li className="space-y-2 border-b border-line py-3 last:border-0">
      <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1">
        <span className="text-sm">
          {job.sweep ? `Sweep of ${job.total}: ${job.labels.join(", ")}` : "Research run"} ·{" "}
          {job.start} to {job.end}
        </span>
        <span className="flex items-baseline gap-3 text-xs text-foreground-muted">
          started {formatIstDateTime(job.started_at)} IST
          <Badge tone={TONE[job.status] ?? "muted"}>
            {job.status}
            {job.code ? `: ${job.code}` : ""}
          </Badge>
        </span>
      </div>
      <Progress job={job} />
      {job.outcomes.length > 0 ? (
        <ul className="flex flex-wrap gap-x-5 gap-y-1 text-sm">
          {job.outcomes.map((outcome, index) => (
            <li key={outcome.run_id ?? `${outcome.label}-${index}`}>
              {outcome.run_id ? (
                <Link
                  to={`/hat/backtests/${outcome.run_id}`}
                  className="text-accent hover:underline"
                >
                  {outcome.label ?? "the run"}
                </Link>
              ) : (
                <span>{outcome.label}</span>
              )}{" "}
              <span className="text-xs text-foreground-muted">
                {outcome.status}
                {outcome.code ? `: ${outcome.code}` : ""}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </li>
  );
}

/** The jobs started since the server started, polled while one runs; hidden when there are none. */
export function JobsPanel({ className }: Readonly<{ className?: string }>) {
  const jobs = useBacktestJobs();
  if (!jobs.data || jobs.data.length === 0) return null;
  return (
    <section aria-label="Jobs" className={className}>
      <h2 className="mb-1 text-lg">Jobs</h2>
      <ul>
        {jobs.data.map((job) => (
          <JobItem key={job.id} job={job} />
        ))}
      </ul>
    </section>
  );
}
