import { getTradingViewUrl } from "@/lib/tradingview";

export function openTradingView(symbol: string) {
  globalThis.open(getTradingViewUrl(symbol), "_blank", "noopener,noreferrer");
}
