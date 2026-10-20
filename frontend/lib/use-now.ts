import { useCallback, useSyncExternalStore } from "react";

/**
 * The current time in epoch ms, floored to `intervalMs`, re-rendering the caller once per
 * interval while it is mounted. Lets render-time logic depend on the clock without an effect.
 */
export function useNow(intervalMs: number): number {
  const subscribe = useCallback(
    (onChange: () => void) => {
      const timer = setInterval(onChange, intervalMs);
      return () => clearInterval(timer);
    },
    [intervalMs],
  );
  const getSnapshot = useCallback(
    () => Math.floor(Date.now() / intervalMs) * intervalMs,
    [intervalMs],
  );
  return useSyncExternalStore(subscribe, getSnapshot);
}
