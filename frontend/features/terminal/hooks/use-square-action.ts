import { useSquarePosition, type PositionItem } from "@/lib/queries/terminal";
import { errorText, squareMessage } from "../lib/terminal-view";

/** Squares a position and reports the outcome through the order ticket message. */
export function useSquareAction(onMessage: (message: string | null) => void) {
  const square = useSquarePosition();
  return {
    squaring: square.isPending,
    squarePosition: (row: PositionItem) => {
      onMessage(null);
      // A new click is a new intent; retries of this mutation reuse this id.
      square.mutate(
        { row, clientOrderId: crypto.randomUUID() },
        {
          onSuccess: (result) => onMessage(squareMessage(row, result)),
          onError: (error) => onMessage(errorText(error, "Failed to square position")),
        },
      );
    },
  };
}
