/** Central query-key factory. Keys go general to specific so prefixes can be invalidated. */
export const queryKeys = {
  terminal: {
    all: ["terminal"] as const,
    symbolSearch: (term: string) => ["terminal", "symbol-search", term] as const,
    watchlist: () => ["terminal", "watchlist"] as const,
    positions: () => ["terminal", "positions"] as const,
    riskCheck: (order: object, openId: number) =>
      ["terminal", "risk-check", order, openId] as const,
  },
  auth: {
    all: ["auth"] as const,
    fyersStatus: () => ["auth", "fyers", "status"] as const,
  },
  dashboard: {
    all: ["dashboard"] as const,
    marketStatus: () => ["dashboard", "market-status"] as const,
    portfolioStats: () => ["dashboard", "portfolio-stats"] as const,
    watchlist: () => ["dashboard", "watchlist"] as const,
    indicesRoot: () => ["dashboard", "indices"] as const,
    indices: (marketOpen: boolean) => ["dashboard", "indices", { marketOpen }] as const,
    marketInsights: () => ["dashboard", "market-insights"] as const,
    postMarketInsights: () => ["dashboard", "post-market-insights"] as const,
  },
};
