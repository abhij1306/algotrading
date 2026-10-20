import { useState } from "react";
import {
  useCloseStrategyPosition,
  useRunStrategyBacktest,
  useRunStrategyScan,
  useToggleSignalQueue,
  useToggleStrategyHalt,
  type ScanRow,
  type StrategyBacktestResult,
} from "@/lib/queries/strategies";
import type { BacktestFormValues as BacktestForm } from "@/components/strategies/BacktestPanel";
import { parseCapital, type TimedError } from "@/components/strategies/strategy-view";

const DEFAULT_BACKTEST_FORM: BacktestForm = {
  startDate: "2025-01-01",
  endDate: "2026-03-13",
  capital: "1000000",
};

/**
 * The strategy actions (scan, halt, queue, close, backtest) and the error the last one left.
 * `busy` is true while any of them is in flight, so every action button waits for it; closing a
 * position twice would overwrite the recorded P&L.
 */
export function useStrategyActions(universe: string) {
  const [actionError, setActionError] = useState<TimedError | null>(null);
  const [backtestForm, setBacktestForm] = useState<BacktestForm>(DEFAULT_BACKTEST_FORM);
  const [backtestResult, setBacktestResult] = useState<StrategyBacktestResult | null>(null);

  const runScan = useRunStrategyScan();
  const toggleHalt = useToggleStrategyHalt();
  const toggleQueue = useToggleSignalQueue();
  const closePosition = useCloseStrategyPosition();
  const runBacktest = useRunStrategyBacktest();
  const busy = [runScan, toggleHalt, toggleQueue, closePosition, runBacktest].some(
    (mutation) => mutation.isPending,
  );

  const failAction = (failure: Error | string) => {
    const message = typeof failure === "string" ? failure : failure.message;
    setActionError({ message, at: Date.now() });
  };
  const clearError = () => setActionError(null);
  const callbacks = { onSuccess: clearError, onError: failAction };

  return {
    busy,
    actionError,
    clearError,
    backtestForm,
    backtestResult,
    editBacktest: (change: Partial<BacktestForm>) =>
      setBacktestForm((form) => ({ ...form, ...change })),
    runScan: () => {
      setActionError(null);
      runScan.mutate(universe, { onError: failAction });
    },
    toggleHalt: (halted: boolean) => toggleHalt.mutate(halted ? "resume" : "halt", callbacks),
    toggleQueue: (row: ScanRow) => toggleQueue.mutate(row, callbacks),
    closePosition: (positionId: number) => closePosition.mutate(positionId, callbacks),
    runBacktest: () => {
      const initialCapital = parseCapital(backtestForm.capital);
      if (initialCapital === null) {
        failAction("Enter a valid numeric capital amount before running the backtest.");
        return;
      }
      runBacktest.mutate(
        {
          universe,
          startDate: backtestForm.startDate,
          endDate: backtestForm.endDate,
          initialCapital,
        },
        {
          onSuccess: (result) => {
            setBacktestResult(result);
            setActionError(null);
          },
          onError: failAction,
        },
      );
    },
  };
}
