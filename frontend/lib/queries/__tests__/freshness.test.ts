import { describe, expect, it } from "vite-plus/test";
import { freshness } from "../freshness";

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
