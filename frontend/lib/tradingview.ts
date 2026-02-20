export function toTradingViewSymbol(symbol: string): string {
  const raw = String(symbol || "").trim().toUpperCase();
  if (!raw) return "NSE:NIFTY";

  if (raw === "NIFTY50") return "NSE:NIFTY";
  if (raw === "BANKNIFTY") return "NSE:BANKNIFTY";
  if (raw === "SENSEX") return "BSE:SENSEX";

  if (raw.includes(":")) {
    const [exchange, value] = raw.split(":");
    const ticker = (value || "").replace(/-EQ$|-INDEX$/g, "").split("-")[0];
    return `${exchange}:${ticker}`;
  }

  const ticker = raw.replace(/-EQ$|-INDEX$/g, "").split("-")[0];
  return `NSE:${ticker}`;
}

export function getTradingViewUrl(symbol: string): string {
  const tvSymbol = toTradingViewSymbol(symbol);
  return `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(tvSymbol)}`;
}
