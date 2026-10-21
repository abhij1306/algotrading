import { describe, expect, it } from "vite-plus/test";
import { formatIstClock } from "../dashboard";

describe("formatIstClock", () => {
  it.each([
    ["2026-10-20T03:45:00Z", "09:15:00 IST"],
    ["2026-10-16T18:30:00Z", "00:00:00 IST"],
    ["2026-10-16T18:29:59Z", "23:59:59 IST"],
  ])("%s is %s", (iso, expected) => {
    expect(formatIstClock(new Date(iso))).toBe(expected);
  });
});
