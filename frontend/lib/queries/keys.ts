/** Central query-key factory. Keys go general to specific so prefixes can be invalidated. */
export const queryKeys = {
  screener: {
    all: ["screener"] as const,
    indices: () => ["screener", "indices"] as const,
    results: (params: {
      universe: string;
      page: number;
      limit: number;
      sortBy: string;
      sortOrder: string;
      query: string;
    }) => ["screener", "results", params] as const,
  },
  backtest: {
    all: ["backtest"] as const,
    status: () => ["backtest", "status"] as const,
    strategies: () => ["backtest", "strategies"] as const,
    runs: () => ["backtest", "runs"] as const,
    result: (jobId: string) => ["backtest", "result", jobId] as const,
  },
  strategies: {
    all: ["strategies"] as const,
    workspaceRoot: () => ["strategies", "workspace"] as const,
    workspace: (params: { universe: string; showAll: boolean }) =>
      ["strategies", "workspace", params] as const,
    signal: (symbol: string) => ["strategies", "signal", symbol] as const,
  },
  terminal: {
    all: ["terminal"] as const,
    symbolSearch: (term: string) => ["terminal", "symbol-search", term] as const,
    watchlist: () => ["terminal", "watchlist"] as const,
    optionsBoard: (params: { underlying: string; expiry: string }) =>
      ["terminal", "options-board", params] as const,
    positions: () => ["terminal", "positions"] as const,
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
    commodities: () => ["dashboard", "commodities"] as const,
  },
};
