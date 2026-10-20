/**
 * IST (Asia/Kolkata) dates and times: the one owner for wall-clock display on the frontend.
 *
 * Everything goes through Intl.DateTimeFormat with timeZone "Asia/Kolkata", so the output is the
 * same whatever zone the machine is in. Inputs are instants: a Date, epoch milliseconds or an ISO
 * string (one without an offset is read as IST). An invalid input renders as PLACEHOLDER.
 */

import { PLACEHOLDER } from "./format";

export const IST_TIME_ZONE = "Asia/Kolkata";

export type DateInput = Date | number | string;

/** 0 = Sunday ... 6 = Saturday, as Date#getDay numbers them. */
export type DayOfWeek = 0 | 1 | 2 | 3 | 4 | 5 | 6;

/** The IST wall-clock reading of an instant. Month is 1-12. */
export interface IstParts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
  dayOfWeek: DayOfWeek;
}

const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
] as const;

const WEEKDAYS: Readonly<Record<string, DayOfWeek>> = {
  Sun: 0,
  Mon: 1,
  Tue: 2,
  Wed: 3,
  Thu: 4,
  Fri: 5,
  Sat: 6,
};

const istFormatter = new Intl.DateTimeFormat("en-US", {
  timeZone: IST_TIME_ZONE,
  year: "numeric",
  month: "numeric",
  day: "numeric",
  hour: "numeric",
  minute: "numeric",
  second: "numeric",
  weekday: "short",
  hourCycle: "h23",
});

/** "2026-10-20T09:15" or "2026-10-20 09:15:00.5": a date-time with no zone or offset. */
const NAIVE_DATE_TIME = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)$/;

/**
 * The instant an input names. A date-time string with no offset is read as IST wall-clock time
 * (the exchange's zone), not the machine's zone, so it means the same instant everywhere. A
 * date-only string is UTC midnight, as Date parses it, which is the same IST calendar date.
 */
function toDate(input: DateInput): Date | null {
  let date: Date;
  if (input instanceof Date) {
    date = input;
  } else if (typeof input === "string") {
    const naive = NAIVE_DATE_TIME.exec(input.trim());
    date = new Date(naive === null ? input : `${naive[1]}T${naive[2]}+05:30`);
  } else {
    date = new Date(input);
  }
  return Number.isNaN(date.getTime()) ? null : date;
}

function partsOf(date: Date): IstParts {
  const values = new Map<string, string>();
  for (const part of istFormatter.formatToParts(date)) values.set(part.type, part.value);
  const number = (type: string): number => Number(values.get(type));
  return {
    year: number("year"),
    month: number("month"),
    day: number("day"),
    hour: number("hour"),
    minute: number("minute"),
    second: number("second"),
    dayOfWeek: WEEKDAYS[values.get("weekday") ?? ""] ?? 0,
  };
}

/** The IST calendar date, time and day of week of an instant, or null for an invalid input. */
export function istParts(input: DateInput): IstParts | null {
  const date = toDate(input);
  return date === null ? null : partsOf(date);
}

/** The IST day of week (0 = Sunday), or null for an invalid input. */
export function istDayOfWeek(input: DateInput): DayOfWeek | null {
  return istParts(input)?.dayOfWeek ?? null;
}

/** True on Saturday and Sunday in IST. */
export function isIstWeekend(input: DateInput): boolean {
  const day = istDayOfWeek(input);
  return day === 0 || day === 6;
}

const pad2 = (value: number): string => String(value).padStart(2, "0");

function dateText(parts: IstParts): string {
  return `${pad2(parts.day)} ${MONTHS[parts.month - 1]} ${parts.year}`;
}

function timeText(parts: IstParts): string {
  return `${pad2(parts.hour)}:${pad2(parts.minute)}:${pad2(parts.second)}`;
}

/** "05 Mar 2026" in IST. */
export function formatIstDate(input: DateInput): string {
  const parts = istParts(input);
  return parts === null ? PLACEHOLDER : dateText(parts);
}

/** "09:15:00" in IST, 24-hour. */
export function formatIstTime(input: DateInput): string {
  const parts = istParts(input);
  return parts === null ? PLACEHOLDER : timeText(parts);
}

/** "05 Mar 2026, 09:15:00" in IST, 24-hour. */
export function formatIstDateTime(input: DateInput): string {
  const parts = istParts(input);
  return parts === null ? PLACEHOLDER : `${dateText(parts)}, ${timeText(parts)}`;
}

const RELATIVE_STEPS = [
  { unit: "d", seconds: 86_400 },
  { unit: "h", seconds: 3_600 },
  { unit: "m", seconds: 60 },
] as const;

/**
 * How long ago (or until) an instant is, relative to `now`: "just now" under a minute, then
 * "5m ago", "3h ago", "2d ago", or "in 5m" for the future. Whole units, rounded down.
 */
export function formatRelative(input: DateInput, now: DateInput = Date.now()): string {
  const date = toDate(input);
  const reference = toDate(now);
  if (date === null || reference === null) return PLACEHOLDER;
  const deltaSeconds = Math.trunc((reference.getTime() - date.getTime()) / 1000);
  const magnitude = Math.abs(deltaSeconds);
  const step = RELATIVE_STEPS.find((candidate) => magnitude >= candidate.seconds);
  if (step === undefined) return "just now";
  const amount = `${Math.floor(magnitude / step.seconds)}${step.unit}`;
  return deltaSeconds >= 0 ? `${amount} ago` : `in ${amount}`;
}
