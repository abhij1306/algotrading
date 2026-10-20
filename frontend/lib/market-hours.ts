/**
 * NSE market hours from the local clock, read in IST through lib/time, so the answer does not
 * depend on the machine's time zone.
 *
 * The regular session runs from 09:15:00.000 to 15:30:00.000 IST, Monday to Friday, both ends
 * inclusive.
 * Exchange holidays are not modelled: the repo has no NSE holiday calendar, so a weekday holiday
 * reads as a normal session day. The backend's /api/market/status is the better source when it
 * is reachable.
 */

import { formatIstTime, istParts } from "./time";

export interface MarketStatus {
  isOpen: boolean;
  message: string;
  nextSession?: string;
}

const SESSION_OPEN_SECOND = 9 * 3600 + 15 * 60;
const SESSION_CLOSE_SECOND = 15 * 3600 + 30 * 60;
const FRIDAY = 5;

/** Whether the regular NSE session is open at `now` (default: the current instant). */
export function isMarketOpen(now: Date = new Date()): MarketStatus {
  const parts = istParts(now);
  if (parts === null) return { isOpen: false, message: "Market Closed" };
  if (parts.dayOfWeek === 0 || parts.dayOfWeek === 6) {
    return { isOpen: false, message: "Market Closed (Weekend)", nextSession: "Monday 9:15 AM" };
  }

  const second = parts.hour * 3600 + parts.minute * 60 + parts.second;
  // IST is a whole-minute offset from UTC, so the UTC millisecond is the IST millisecond.
  const pastClose =
    second > SESSION_CLOSE_SECOND ||
    (second === SESSION_CLOSE_SECOND && now.getUTCMilliseconds() > 0);
  if (second < SESSION_OPEN_SECOND) {
    return { isOpen: false, message: "Market Closed (Pre-market)", nextSession: "Today 9:15 AM" };
  }
  if (pastClose) {
    return {
      isOpen: false,
      message: "Market Closed (Post-market)",
      nextSession: parts.dayOfWeek === FRIDAY ? "Monday 9:15 AM" : "Tomorrow 9:15 AM",
    };
  }
  return { isOpen: true, message: "Market Open" };
}

/** The IST wall-clock time at `now`, as "14:05:09 IST". */
export function getISTTime(now: Date = new Date()): string {
  return `${formatIstTime(now)} IST`;
}
