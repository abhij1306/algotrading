import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * Utility function to merge Tailwind CSS classes
 * Combines clsx for conditional classes and tailwind-merge for deduplication
 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/**
 * Format a number as currency
 */
export function formatCurrency(
  value: number | null | undefined,
  currency: string = "INR",
  locale: string = "en-IN"
): string {
  if (value === null || value === undefined || isNaN(value)) return '₹0.00';
  return new Intl.NumberFormat(locale, {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

/**
 * Format a number as percentage
 */
export function formatPercent(value: number | null | undefined, decimals: number = 2): string {
  if (value === null || value === undefined || isNaN(value)) return '0.00%';
  const sign = value >= 0 ? "+" : "";
  return `${sign}${value.toFixed(decimals)}%`;
}

/**
 * Alias for formatPercent for consistency with design system
 */
export function formatPercentage(value: number | null | undefined, decimals: number = 2): string {
  return formatPercent(value, decimals);
}

/**
 * Format a large number with abbreviations (K, M, B, T)
 */
export function formatCompact(value: number | null | undefined): string {
  if (value === null || value === undefined || isNaN(value)) return '0';
  if (Math.abs(value) >= 1e7) return `₹${(value / 1e7).toFixed(2)}Cr`; // Crores for Indian market
  if (Math.abs(value) >= 1e5) return `₹${(value / 1e5).toFixed(2)}L`; // Lakhs for Indian market
  if (Math.abs(value) >= 1e3) return `₹${(value / 1e3).toFixed(2)}K`;
  return `₹${value.toFixed(2)}`;
}

/**
 * Alias for formatCompact for consistency with design system
 */
export function formatLargeNumber(value: number | null | undefined): string {
  return formatCompact(value);
}

/**
 * Format price with appropriate decimal places
 */
export function formatPrice(value: number): string {
  if (value >= 1000) return value.toFixed(2);
  if (value >= 1) return value.toFixed(3);
  return value.toFixed(6);
}

/**
 * Round a numeric value to fixed decimals and return as number.
 * Useful for keeping realtime UI values deterministic with provider precision.
 */
export function roundToDecimals(value: number | null | undefined, decimals: number = 2): number {
  if (value === null || value === undefined || isNaN(value)) return 0;
  return Number(value.toFixed(decimals));
}
