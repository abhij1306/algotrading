export function toTradingViewSymbol(symbol: string): string {
  const raw = String(symbol || "").trim().toUpperCase();
  if (!raw) return "NSE:NIFTY";

  if (raw === "NIFTY50") return "NSE:NIFTY";
  if (raw === "BANKNIFTY") return "NSE:BANKNIFTY";
  if (raw === "SENSEX") return "BSE:SENSEX";

  const normalizeTicker = (value: string): string => {
    // Keep hyphenated tickers intact; only trim known suffixes and optional exchange-like prefixes.
    const withoutSuffix = value.replace(/-EQ$|-INDEX$/, "");
    const prefixed = withoutSuffix.match(/^([A-Z0-9]{1,5})-(.+)$/);
    if (prefixed) {
      return prefixed[2];
    }
    return withoutSuffix;
  };

  if (raw.includes(":")) {
    const [exchange, value] = raw.split(":", 2);
    const ticker = normalizeTicker(value || "");
    return `${exchange}:${ticker}`;
  }

  const ticker = normalizeTicker(raw);
  return `NSE:${ticker}`;
}

export function getTradingViewUrl(symbol: string): string {
  const tvSymbol = toTradingViewSymbol(symbol);
  return `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(tvSymbol)}`;
}
