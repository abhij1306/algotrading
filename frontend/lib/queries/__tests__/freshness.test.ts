import { describe, expect, it } from "vite-plus/test";
import { freshness, sharedStale } from "../freshness";

const failure = new Error("Service unavailable");

describe("freshness", () => {
  it("is loading before the first answer and an error when the first read fails", () => {
    expect(freshness({ data: undefined, error: null })).toBe("loading");
    expect(freshness({ data: undefined, error: failure })).toBe("error");
  });

  it("is stale when a refetch fails after a good read, fresh otherwise", () => {
    expect(freshness({ data: { kill_account: false }, error: failure })).toBe("stale");
    expect(freshness({ data: { kill_account: false }, error: null })).toBe("fresh");
  });

  it("treats an empty list as data", () => {
    expect(freshness({ data: [], error: failure })).toBe("stale");
  });
});

describe("sharedStale", () => {
  const good = { data: [], error: null, dataUpdatedAt: 1_000 };
  const stale = (at: number, message = "HTTP 502: Bad Gateway") => ({
    data: [],
    error: new Error(message),
    dataUpdatedAt: at,
  });

  it("is null when at most one query is stale: that section reports it itself", () => {
    expect(sharedStale([good, good])).toBeNull();
    expect(sharedStale([good, stale(2_000)])).toBeNull();
  });

  it("gives the earliest last good read and the common error when several are stale", () => {
    expect(sharedStale([stale(3_000), good, stale(2_000)])).toEqual({
      since: 2_000,
      message: "HTTP 502: Bad Gateway",
    });
  });

  it("summarises different errors", () => {
    expect(sharedStale([stale(1_000, "a"), stale(1_000, "b")])?.message).toBe("2 reads failed");
  });
});
