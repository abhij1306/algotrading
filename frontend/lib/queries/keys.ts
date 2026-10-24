/** Central query-key factory. Keys go general to specific so prefixes can be invalidated. */
export const queryKeys = {
  auth: {
    all: ["auth"] as const,
    status: (broker: "fyers" | "zerodha") => ["auth", broker, "status"] as const,
  },
  hat: {
    all: ["hat"] as const,
    status: () => ["hat", "status"] as const,
    allocations: () => ["hat", "allocations"] as const,
    watchlist: () => ["hat", "watchlist"] as const,
    positions: () => ["hat", "positions"] as const,
    events: (limit: number) => ["hat", "events", limit] as const,
  },
};
