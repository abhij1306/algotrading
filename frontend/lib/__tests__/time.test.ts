import { describe, expect, it } from "vite-plus/test";
import {
  formatIstDate,
  formatIstDateTime,
  formatIstTime,
  formatRelative,
  isIstWeekend,
  istDayOfWeek,
  istParts,
} from "../time";

/** A UTC instant, so every case is independent of the machine's time zone. */
const utc = (iso: string): Date => new Date(`${iso}Z`);

describe("IST wall clock from UTC instants", () => {
  it.each([
    // 18:29:59 UTC is 23:59:59 IST, still Friday.
    ["2026-10-16T18:29:59", "16 Oct 2026", "23:59:59", 5],
    // 18:30:00 UTC is IST midnight: the date and weekday roll over to Saturday.
    ["2026-10-16T18:30:00", "17 Oct 2026", "00:00:00", 6],
    // 00:00 UTC Monday is already 05:30 IST Monday.
    ["2026-10-19T00:00:00", "19 Oct 2026", "05:30:00", 1],
    // Sunday 20:00 UTC is Monday 01:30 IST.
    ["2026-10-18T20:00:00", "19 Oct 2026", "01:30:00", 1],
    // New Year rolls over in IST before it does in UTC.
    ["2025-12-31T18:30:00", "01 Jan 2026", "00:00:00", 4],
    ["2026-03-05T03:45:00", "05 Mar 2026", "09:15:00", 4],
  ])("%s UTC is %s %s IST, weekday %s", (iso, date, time, weekday) => {
    const instant = utc(iso);
    expect(formatIstDate(instant)).toBe(date);
    expect(formatIstTime(instant)).toBe(time);
    expect(formatIstDateTime(instant)).toBe(`${date}, ${time}`);
    expect(istDayOfWeek(instant)).toBe(weekday);
  });

  it("reads epoch milliseconds and ISO strings with an offset", () => {
    expect(formatIstDateTime(Date.UTC(2026, 9, 20, 4, 0, 0))).toBe("20 Oct 2026, 09:30:00");
    expect(formatIstDateTime("2026-10-20T09:30:00+05:30")).toBe("20 Oct 2026, 09:30:00");
  });

  it("returns the IST parts of an instant", () => {
    expect(istParts(utc("2026-10-16T18:30:05"))).toEqual({
      year: 2026,
      month: 10,
      day: 17,
      hour: 0,
      minute: 0,
      second: 5,
      dayOfWeek: 6,
    });
  });

  it.each([["not a date"], [Number.NaN], [new Date(Number.NaN)]])(
    "renders the invalid input %s as the placeholder",
    (input) => {
      expect(formatIstDate(input)).toBe("--");
      expect(formatIstTime(input)).toBe("--");
      expect(formatIstDateTime(input)).toBe("--");
      expect(istParts(input)).toBe(null);
      expect(istDayOfWeek(input)).toBe(null);
    },
  );
});

describe("isIstWeekend", () => {
  it.each([
    ["2026-10-16T18:29:59", false], // Fri 23:59:59 IST
    ["2026-10-16T18:30:00", true], // Sat 00:00 IST
    ["2026-10-18T18:29:59", true], // Sun 23:59:59 IST
    ["2026-10-18T18:30:00", false], // Mon 00:00 IST
  ])("%s UTC is a weekend in IST: %s", (iso, expected) => {
    expect(isIstWeekend(utc(iso))).toBe(expected);
  });

  it("is false for an invalid input", () => {
    expect(isIstWeekend("not a date")).toBe(false);
  });
});

describe("formatRelative", () => {
  const now = utc("2026-10-20T10:00:00");
  it.each([
    ["2026-10-20T10:00:00", "just now"],
    ["2026-10-20T09:59:01", "just now"],
    ["2026-10-20T09:59:00", "1m ago"],
    ["2026-10-20T09:15:30", "44m ago"],
    ["2026-10-20T07:00:00", "3h ago"],
    ["2026-10-18T09:00:00", "2d ago"],
    ["2026-10-20T10:05:00", "in 5m"],
    ["2026-10-21T10:00:00", "in 1d"],
  ])("%s UTC is %s", (iso, expected) => {
    expect(formatRelative(utc(iso), now)).toBe(expected);
  });

  it("renders an invalid instant as the placeholder", () => {
    expect(formatRelative("not a date", now)).toBe("--");
  });
});
