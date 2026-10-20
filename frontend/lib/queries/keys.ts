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
};
