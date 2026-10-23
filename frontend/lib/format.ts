/**
 * Number formatting: the one owner for display strings on the frontend.
 *
 * The formatter takes `number | null | undefined` and returns PLACEHOLDER for null, undefined,
 * NaN and ±Infinity, so a missing value never renders as a fake zero. Grouping is Indian
 * (1,23,45,678) via the "en-IN" locale, never the machine's locale. A value that rounds to zero
 * never renders with a minus sign.
 */

export const PLACEHOLDER = "--";

type MaybeNumber = number | null | undefined;

const formatters = new Map<number, Intl.NumberFormat>();

/**
 * An en-IN formatter, built once per decimal count. signDisplay "negative" drops the minus sign
 * from values that round to zero.
 */
function enIn(decimals: number): Intl.NumberFormat {
  let formatter = formatters.get(decimals);
  if (formatter === undefined) {
    formatter = new Intl.NumberFormat("en-IN", {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
      signDisplay: "negative",
    });
    formatters.set(decimals, formatter);
  }
  return formatter;
}

/** "1,23,456.78": Indian grouping with exactly `decimals` fraction digits. */
export function formatNumber(value: MaybeNumber, decimals: number = 2): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return PLACEHOLDER;
  return enIn(decimals).format(value);
}
