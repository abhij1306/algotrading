import { Loader2 } from "lucide-react";
import { formatNumber } from "@/lib/format";
import type { OptionsBoard, OptionsOrderflow } from "@/lib/queries/terminal";
import type { OptionsChain } from "../hooks/use-options-chain";

function OptionsBoardPanel({
  board: optionsBoard,
  orderflow: optionsOrderflow,
  underlying: optionsUnderlying,
  expiry: optionsExpiry,
  onUnderlyingChange: setOptionsUnderlying,
  onExpiryChange: setOptionsExpiry,
  onSelectOption: setSelectedOptionSymbol,
}: Readonly<{
  board: OptionsBoard;
  orderflow: OptionsOrderflow | null;
  underlying: string;
  expiry: string;
  onUnderlyingChange: (value: string) => void;
  onExpiryChange: (value: string) => void;
  onSelectOption: (symbol: string) => void;
}>) {
  return (
    <div className="h-full w-full overflow-auto p-2">
      <div className="mb-2 flex items-center gap-2">
        <select
          value={optionsUnderlying}
          onChange={(event) => setOptionsUnderlying(event.target.value)}
          className="h-8 rounded border border-border bg-background px-2 text-xs"
        >
          <option value="NIFTY">NIFTY</option>
          <option value="BANKNIFTY">BANKNIFTY</option>
          <option value="FINNIFTY">FINNIFTY</option>
        </select>
        <input
          type="date"
          value={optionsExpiry}
          onChange={(event) => setOptionsExpiry(event.target.value)}
          className="h-8 rounded border border-border bg-background px-2 text-xs"
        />
        {optionsOrderflow && (
          <div className="text-xs text-foreground-muted">
            PCR(OI): {optionsOrderflow.pcr_oi ?? "--"} | PCR(Vol):{" "}
            {optionsOrderflow.pcr_volume ?? "--"}
          </div>
        )}
      </div>
      <table className="w-full text-xs">
        <thead className="border-b border-border text-foreground-muted">
          <tr>
            <th className="py-1 text-left font-normal">CE</th>
            <th className="py-1 text-right font-normal">CE LTP</th>
            <th className="py-1 text-right font-normal">Strike</th>
            <th className="py-1 text-right font-normal">PE LTP</th>
            <th className="py-1 text-left font-normal">PE</th>
          </tr>
        </thead>
        <tbody>
          {optionsBoard.strikes.map((row) => (
            <tr key={row.strike} className="border-b border-border/60">
              <td className="py-1">
                <button
                  type="button"
                  className="text-left hover:text-profit"
                  onClick={() => {
                    if (row.ce?.symbol) {
                      setSelectedOptionSymbol(row.ce.symbol);
                    }
                  }}
                >
                  {row.ce?.symbol || "--"}
                </button>
              </td>
              <td className="py-1 text-right tabular-nums">{formatNumber(row.ce?.ltp)}</td>
              <td
                className={`py-1 text-right tabular-nums ${
                  row.strike === optionsBoard.atm_strike
                    ? "text-foreground font-semibold"
                    : "text-foreground-muted"
                }`}
              >
                {formatNumber(row.strike)}
              </td>
              <td className="py-1 text-right tabular-nums">{formatNumber(row.pe?.ltp)}</td>
              <td className="py-1 text-left">
                <button
                  type="button"
                  className="text-left hover:text-loss"
                  onClick={() => {
                    if (row.pe?.symbol) {
                      setSelectedOptionSymbol(row.pe.symbol);
                    }
                  }}
                >
                  {row.pe?.symbol || "--"}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The option chain, or its loading, error or empty state. */
export function OptionChain({ chain }: Readonly<{ chain: OptionsChain }>) {
  const { snapshot } = chain;
  if (snapshot) {
    return (
      <OptionsBoardPanel
        board={snapshot.board}
        orderflow={snapshot.orderflow}
        underlying={chain.underlying}
        expiry={chain.expiry}
        onUnderlyingChange={chain.setUnderlying}
        onExpiryChange={chain.setExpiry}
        onSelectOption={chain.setSelectedOptionSymbol}
      />
    );
  }
  if (chain.isLoading) {
    return (
      <div className="text-center">
        <Loader2 className="w-8 h-8 animate-spin mx-auto mb-2 text-foreground-muted" />
        <p className="text-sm text-foreground-muted">Loading options board...</p>
      </div>
    );
  }
  return (
    <div className="text-center">
      {chain.error ? (
        <p className="text-sm text-loss">{chain.error}</p>
      ) : (
        <p className="text-sm text-foreground-muted">No options data</p>
      )}
    </div>
  );
}
