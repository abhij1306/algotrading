import { describe, expect, it } from "vite-plus/test";
import { formatIstDate, formatIstDateTime, formatIstTime } from "../time";

/** A UTC instant, so every case is independent of the machine's time zone. */
const utc = (iso: string): Date => new Date(`${iso}Z`);

describe("IST wall clock from UTC instants", () => {
  it.each([
    // 18:29:59 UTC is 23:59:59 IST, still the same day.
    ["2026-10-16T18:29:59", "16 Oct 2026", "23:59:59"],
    // 18:30:00 UTC is IST midnight: the date rolls over.
    ["2026-10-16T18:30:00", "17 Oct 2026", "00:00:00"],
    // 00:00 UTC is already 05:30 IST.
    ["2026-10-19T00:00:00", "19 Oct 2026", "05:30:00"],
    // 20:00 UTC is 01:30 IST the next day.
    ["2026-10-18T20:00:00", "19 Oct 2026", "01:30:00"],
    // New Year rolls over in IST before it does in UTC.
    ["2025-12-31T18:30:00", "01 Jan 2026", "00:00:00"],
    ["2026-03-05T03:45:00", "05 Mar 2026", "09:15:00"],
  ])("%s UTC is %s %s IST", (iso, date, time) => {
    const instant = utc(iso);
    expect(formatIstDate(instant)).toBe(date);
    expect(formatIstTime(instant)).toBe(time);
    expect(formatIstDateTime(instant)).toBe(`${date}, ${time}`);
  });

  it.each([
    ["2026-10-20T09:15:00", "20 Oct 2026, 09:15:00"],
    ["2026-10-20 09:15", "20 Oct 2026, 09:15:00"],
    ["2026-10-20T23:59:59.999", "20 Oct 2026, 23:59:59"],
  ])("reads the offset-less date-time %s as IST wall-clock time", (input, expected) => {
    expect(formatIstDateTime(input)).toBe(expected);
  });

  it("reads a date-only string as that IST calendar date", () => {
    expect(formatIstDate("2026-10-20")).toBe("20 Oct 2026");
  });

  it("reads epoch milliseconds and ISO strings with an offset", () => {
    expect(formatIstDateTime(Date.UTC(2026, 9, 20, 4, 0, 0))).toBe("20 Oct 2026, 09:30:00");
    expect(formatIstDateTime("2026-10-20T09:30:00+05:30")).toBe("20 Oct 2026, 09:30:00");
  });

  it.each([["not a date"], [Number.NaN], [new Date(Number.NaN)]])(
    "renders the invalid input %s as the placeholder",
    (input) => {
      expect(formatIstDate(input)).toBe("--");
      expect(formatIstTime(input)).toBe("--");
      expect(formatIstDateTime(input)).toBe("--");
    },
  );
});
