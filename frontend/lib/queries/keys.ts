/** Central query-key factory. Keys go general to specific so prefixes can be invalidated. */
export const queryKeys = {
  auth: {
    all: ["auth"] as const,
    fyersStatus: () => ["auth", "fyers", "status"] as const,
  },
  dashboard: {
    all: ["dashboard"] as const,
    marketStatus: () => ["dashboard", "market-status"] as const,
    watchlist: () => ["dashboard", "watchlist"] as const,
    indicesRoot: () => ["dashboard", "indices"] as const,
    indices: (marketOpen: boolean) => ["dashboard", "indices", { marketOpen }] as const,
    marketInsights: () => ["dashboard", "market-insights"] as const,
    postMarketInsights: () => ["dashboard", "post-market-insights"] as const,
  },
};
