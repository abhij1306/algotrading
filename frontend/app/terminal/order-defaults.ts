export type TradingMode = "PAPER" | "LIVE";
export type OrderType = "MARKET" | "LIMIT" | "SL" | "SL-M";

// Every page load starts in PAPER. LIVE is never restored from storage.
export const DEFAULT_TRADING_MODE: TradingMode = "PAPER";
// LIMIT bounds the fill price; the user must opt in to MARKET.
export const DEFAULT_ORDER_TYPE: OrderType = "LIMIT";
