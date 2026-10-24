import { useSharedStale } from "@/lib/queries/hat";
import { formatIstTime } from "@/lib/time";

/** One page-level notice when several sections went stale together, e.g. the backend is down. */
export function StaleBanner() {
  const stale = useSharedStale();
  if (stale === null) return null;
  return (
    <p role="alert" className="mb-10 rounded-md bg-warning-bg px-4 py-3 text-sm text-warning">
      Not refreshing since {formatIstTime(stale.since)} IST ({stale.message}). The sections below
      show their last good read, dimmed.
    </p>
  );
}
