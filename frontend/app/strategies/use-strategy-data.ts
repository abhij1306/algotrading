import { useMemo, useState } from "react";
import { useScreenerIndices } from "@/lib/queries/screener";
import {
  useStrategiesWorkspace,
  useStrategySignal,
  type BacktestRun,
  type PositionRow,
  type ScanRow,
} from "@/lib/queries/strategies";
import {
  resolveUniverse,
  splitSignals,
  timedQueryError,
  type TimedError,
  type UniverseOption,
} from "@/components/strategies/strategy-view";

const NO_UNIVERSES: UniverseOption[] = [];
const NO_SCAN_ROWS: ScanRow[] = [];
const NO_POSITIONS: PositionRow[] = [];
const NO_RUNS: BacktestRun[] = [];

/**
 * What the strategies page reads: the universe and view choices, the workspace for that universe,
 * and the signal shown in the detail panel.
 */
export function useStrategyData() {
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [universeChoice, setUniverseChoice] = useState("NIFTY500");

  const universesQuery = useScreenerIndices();
  const universes: UniverseOption[] = universesQuery.data ?? NO_UNIVERSES;
  const universe = resolveUniverse(universes, universeChoice);

  const workspaceQuery = useStrategiesWorkspace({ universe, showAll });
  const results = workspaceQuery.data?.results ?? NO_SCAN_ROWS;
  const { forming, breakouts } = useMemo(() => splitSignals(results), [results]);

  // The explicitly selected signal, else the first scan result.
  const signalSymbol = selectedSymbol ?? results[0]?.symbol ?? null;
  const signalQuery = useStrategySignal(signalSymbol);
  const signalDetail = signalSymbol === null ? null : (signalQuery.data ?? null);

  // The auto-selected first row fails quietly; only an explicitly picked signal shows its error.
  const queryErrors: TimedError[] = [
    timedQueryError(workspaceQuery.error, workspaceQuery.errorUpdatedAt),
    timedQueryError(selectedSymbol === null ? null : signalQuery.error, signalQuery.errorUpdatedAt),
  ].filter((entry) => entry !== null);

  return {
    universes,
    universe,
    showAll,
    status: workspaceQuery.data?.status ?? null,
    results,
    forming,
    breakouts,
    positions: workspaceQuery.data?.positions ?? NO_POSITIONS,
    runs: workspaceQuery.data?.runs ?? NO_RUNS,
    loading: workspaceQuery.isFetching,
    signalSymbol,
    signalDetail,
    queryErrors,
    chooseUniverse: (id: string) => {
      setUniverseChoice(id);
      setSelectedSymbol(null);
    },
    toggleShowAll: () => setShowAll((value) => !value),
    selectSymbol: setSelectedSymbol,
  };
}
