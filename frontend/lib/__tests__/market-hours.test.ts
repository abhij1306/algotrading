import { describe, expect, it } from "vite-plus/test";
import { getISTTime, isMarketOpen } from "../market-hours";

/** A UTC instant; IST is UTC+05:30, so 09:15 IST is 03:45 UTC and 15:30 IST is 10:00 UTC. */
const utc = (iso: string): Date => new Date(`${iso}Z`);

describe("isMarketOpen", () => {
  it.each([
    // Tuesday 20 Oct 2026, around the open and close.
    ["2026-10-20T03:44:59", "09:14:59 Tue", false, "Market Closed (Pre-market)", "Today 9:15 AM"],
    ["2026-10-20T03:45:00", "09:15:00 Tue", true, "Market Open", undefined],
    ["2026-10-20T07:00:00", "12:30:00 Tue", true, "Market Open", undefined],
    ["2026-10-20T10:00:00", "15:30:00 Tue", true, "Market Open", undefined],
    [
      "2026-10-20T10:00:01",
      "15:30:01 Tue",
      false,
      "Market Closed (Post-market)",
      "Tomorrow 9:15 AM",
    ],
    // UTC is still Monday evening, but IST is already Tuesday morning before the open.
    ["2026-10-19T19:00:00", "00:30:00 Tue", false, "Market Closed (Pre-market)", "Today 9:15 AM"],
    // Friday after the close: the next session is Monday, not tomorrow.
    ["2026-10-16T10:00:01", "15:30:01 Fri", false, "Market Closed (Post-market)", "Monday 9:15 AM"],
    // UTC is still Friday, but IST is Saturday.
    ["2026-10-16T18:30:00", "00:00:00 Sat", false, "Market Closed (Weekend)", "Monday 9:15 AM"],
    ["2026-10-18T07:00:00", "12:30:00 Sun", false, "Market Closed (Weekend)", "Monday 9:15 AM"],
    // UTC is still Sunday, but IST is Monday morning before the open.
    ["2026-10-18T18:30:00", "00:00:00 Mon", false, "Market Closed (Pre-market)", "Today 9:15 AM"],
  ])("%s UTC (%s IST) open=%s", (iso, _ist, isOpen, message, nextSession) => {
    expect(isMarketOpen(utc(iso))).toEqual({ isOpen, message, nextSession });
  });

  it.each([
    ["2026-10-20T10:00:00.000", true],
    ["2026-10-20T10:00:00.001", false],
    ["2026-10-20T10:00:00.500", false],
    ["2026-10-20T03:44:59.999", false],
  ])("%s UTC is open=%s at millisecond precision", (iso, isOpen) => {
    expect(isMarketOpen(utc(iso)).isOpen).toBe(isOpen);
  });

  it("reports closed for an invalid instant", () => {
    expect(isMarketOpen(new Date(Number.NaN))).toEqual({ isOpen: false, message: "Market Closed" });
  });
});

describe("getISTTime", () => {
  it.each([
    ["2026-10-20T03:45:00", "09:15:00 IST"],
    ["2026-10-16T18:30:00", "00:00:00 IST"],
    ["2026-10-16T18:29:59", "23:59:59 IST"],
  ])("%s UTC is %s", (iso, expected) => {
    expect(getISTTime(utc(iso))).toBe(expected);
  });
});
