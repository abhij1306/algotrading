import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";
import { formatInr } from "./format";

/**
 * Utility function to merge Tailwind CSS classes
 * Combines clsx for conditional classes and tailwind-merge for deduplication
 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

// Formatting lives in lib/format.ts. These names stay only for callers not yet switched over
// (dashboard, terminal, lib/queries/terminal.ts and the dashboard insight panels); new code
// imports from "@/lib/format".
export { formatSignedPercent as formatPercentage, roundToDecimals } from "./format";

/** @deprecated Use formatInr from "@/lib/format". Rupees only, so currency and locale are fixed. */
export function formatCurrency(
  value: number | null | undefined,
  _currency: "INR" = "INR",
  _locale: "en-IN" = "en-IN",
): string {
  return formatInr(value);
}
