import { useState } from "react";
import { useOptionsBoard } from "@/lib/queries/terminal";
import {
  errorText,
  optionChainKey,
  shownOptionSymbol,
  type OptionPick,
} from "../lib/terminal-view";

/**
 * Options chain state. Until the user picks an expiry the backend chooses the nearest one,
 * and the board's own expiry is shown as the effective expiry.
 *
 * A picked option belongs to the chain it was picked on: once the underlying or expiry changes,
 * or the contract leaves the shown board, it is derived away on render, so the order ticket
 * never trades a contract that is not on screen.
 */
export function useOptionsChain(enabled: boolean) {
  const [underlying, setUnderlying] = useState("NIFTY");
  const [chosenExpiry, setChosenExpiry] = useState("");
  const [pick, setPick] = useState<OptionPick | null>(null);
  const chain = optionChainKey(underlying, chosenExpiry);
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
    selectedOptionSymbol: shownOptionSymbol(pick, chain, snapshot?.board ?? null),
    setSelectedOptionSymbol: (symbol: string) => setPick({ chain, symbol }),
  };
}

export type OptionsChain = ReturnType<typeof useOptionsChain>;
