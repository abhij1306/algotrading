import { afterEach, beforeEach, describe, expect, it, vi } from "vite-plus/test";
import { debounce } from "../debounce";

describe("debounce", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("calls once with the last arguments after the wait", () => {
    const calls: unknown[][] = [];
    const run = debounce((...args: unknown[]) => calls.push(args), 250);

    run("a");
    run("ab");
    vi.advanceTimersByTime(249);
    expect(calls).toEqual([]);
    vi.advanceTimersByTime(1);
    expect(calls).toEqual([["ab"]]);
  });

  it("cancel drops the pending call", () => {
    const calls: unknown[][] = [];
    const run = debounce((...args: unknown[]) => calls.push(args), 250);

    run("ab");
    run.cancel();
    vi.advanceTimersByTime(1000);
    expect(calls).toEqual([]);

    run("abc");
    vi.advanceTimersByTime(250);
    expect(calls).toEqual([["abc"]]);
  });
});
