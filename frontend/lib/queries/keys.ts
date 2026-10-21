/** Central query-key factory. Keys go general to specific so prefixes can be invalidated. */
export const queryKeys = {
  auth: {
    all: ["auth"] as const,
    fyersStatus: () => ["auth", "fyers", "status"] as const,
  },
};
