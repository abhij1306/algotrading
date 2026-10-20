import { useState } from "react";
import { useOptionsBoard } from "@/lib/queries/terminal";
import { errorText } from "../lib/terminal-view";

/**
 * Options chain state. Until the user picks an expiry the backend chooses the nearest one,
 * and the board's own expiry is shown as the effective expiry.
 */
export function useOptionsChain(enabled: boolean) {
  const [underlying, setUnderlying] = useState("NIFTY");
  const [chosenExpiry, setChosenExpiry] = useState("");
  const [selectedOptionSymbol, setSelectedOptionSymbol] = useState("");
  const boardQuery = useOptionsBoard({ underlying, expiry: chosenExpiry, enabled });
  const snapshot = boardQuery.data ?? null;
  return {
    underlying,
    setUnderlying,
    expiry: chosenExpiry || (snapshot?.board.expiry ?? ""),
    setExpiry: setChosenExpiry,
    snapshot,
    isLoading: boardQuery.isFetching,
    error: boardQuery.error ? errorText(boardQuery.error, "Failed to load options board") : null,
    selectedOptionSymbol,
    setSelectedOptionSymbol,
  };
}

export type OptionsChain = ReturnType<typeof useOptionsChain>;
