/**
 * Number, currency and percent formatting: the one owner for display strings on the frontend.
 *
 * Every formatter takes `number | null | undefined` and returns PLACEHOLDER for null, undefined,
 * NaN and ±Infinity, so a missing value never renders as a fake zero. Grouping is Indian
 * (1,23,45,678) via the "en-IN" locale, never the machine's locale. A value that rounds to zero
 * never renders with a minus sign.
 */

export const PLACEHOLDER = "--";

type MaybeNumber = number | null | undefined;

const CRORE = 1e7;
const LAKH = 1e5;
const THOUSAND = 1e3;

function isFiniteNumber(value: MaybeNumber): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

const formatters = new Map<string, Intl.NumberFormat>();

/**
 * An en-IN formatter, built once per option set. signDisplay "negative" drops the minus sign
 * from values that round to zero.
 */
function enIn(
  minDecimals: number,
  maxDecimals: number,
  style: "decimal" | "currency" = "decimal",
): Intl.NumberFormat {
  const key = `${style}:${minDecimals}:${maxDecimals}`;
  let formatter = formatters.get(key);
  if (formatter === undefined) {
    formatter = new Intl.NumberFormat("en-IN", {
      style,
      currency: "INR",
      minimumFractionDigits: minDecimals,
      maximumFractionDigits: maxDecimals,
      signDisplay: "negative",
    });
    formatters.set(key, formatter);
  }
  return formatter;
}

/**
 * Round to fixed decimals and return a number, so realtime values stay at provider precision.
 * Not a display formatter: a missing value becomes 0.
 */
export function roundToDecimals(value: MaybeNumber, decimals: number = 2): number {
  if (!isFiniteNumber(value)) return 0;
  const factor = 10 ** decimals;
  const epsilon = value >= 0 ? Number.EPSILON : -Number.EPSILON;
  const rounded = Math.round((value + epsilon) * factor) / factor;
  return rounded === 0 ? 0 : rounded;
}

/** "1,23,456.78": Indian grouping with exactly `decimals` fraction digits. */
export function formatNumber(value: MaybeNumber, decimals: number = 2): string {
  if (!isFiniteNumber(value)) return PLACEHOLDER;
  return enIn(decimals, decimals).format(value);
}

/** "₹1,23,456.78" and "-₹1,234.50": rupees with Indian grouping and exactly `decimals` digits. */
export function formatInr(value: MaybeNumber, decimals: number = 2): string {
  if (!isFiniteNumber(value)) return PLACEHOLDER;
  return enIn(decimals, decimals, "currency").format(value);
}

/** "+1.23%", "-0.50%", "+0.00%": the sign is always shown; a value that rounds to zero is "+". */
export function formatSignedPercent(value: MaybeNumber, decimals: number = 2): string {
  if (!isFiniteNumber(value)) return PLACEHOLDER;
  const digits = Math.abs(value).toFixed(decimals);
  const sign = value < 0 && Number(digits) !== 0 ? "-" : "+";
  return `${sign}${digits}%`;
}

/**
 * Compact rupees with Indian units: "₹1.50Cr", "₹12.30L", and below one lakh the grouped amount
 * with up to two fraction digits ("₹45,000", "₹999.5"). `decimals` applies to the Cr and L tiers.
 */
export function formatCompactInr(value: MaybeNumber, decimals: number = 2): string {
  if (!isFiniteNumber(value)) return PLACEHOLDER;
  const magnitude = Math.abs(value);
  const sign = value < 0 ? "-" : "";
  if (magnitude >= CRORE) return `${sign}₹${(magnitude / CRORE).toFixed(decimals)}Cr`;
  if (magnitude >= LAKH) return `${sign}₹${(magnitude / LAKH).toFixed(decimals)}L`;
  return enIn(0, 2, "currency").format(value);
}

/**
 * Compact counts such as volume: "1.2Cr", "3.4L", "5.6K", and below one thousand the whole
 * number ("999"). `decimals` applies to the Cr, L and K tiers.
 */
export function formatCompactNumber(value: MaybeNumber, decimals: number = 1): string {
  if (!isFiniteNumber(value)) return PLACEHOLDER;
  const magnitude = Math.abs(value);
  const sign = value < 0 ? "-" : "";
  if (magnitude >= CRORE) return `${sign}${(magnitude / CRORE).toFixed(decimals)}Cr`;
  if (magnitude >= LAKH) return `${sign}${(magnitude / LAKH).toFixed(decimals)}L`;
  if (magnitude >= THOUSAND) return `${sign}${(magnitude / THOUSAND).toFixed(decimals)}K`;
  return enIn(0, 0).format(value);
}

/**
 * An amount already in crores, such as market cap: "12L Cr", "45K Cr", "820 Cr".
 */
export function formatCrores(value: MaybeNumber): string {
  if (!isFiniteNumber(value)) return PLACEHOLDER;
  const magnitude = Math.abs(value);
  const sign = value < 0 ? "-" : "";
  if (magnitude >= LAKH) return `${sign}${(magnitude / LAKH).toFixed(0)}L Cr`;
  if (magnitude >= THOUSAND) return `${sign}${(magnitude / THOUSAND).toFixed(0)}K Cr`;
  const rounded = Math.round(magnitude);
  return rounded === 0 ? "0 Cr" : `${sign}${rounded} Cr`;
}
