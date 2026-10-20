import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * Utility function to merge Tailwind CSS classes
 * Combines clsx for conditional classes and tailwind-merge for deduplication
 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

// Formatting lives in lib/format.ts. These names stay only for callers not yet switched over
// (terminal and lib/queries/terminal.ts); new code imports from "@/lib/format".
export { formatSignedPercent as formatPercentage, roundToDecimals } from "./format";
