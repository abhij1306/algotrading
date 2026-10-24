import { formatNumber, PLACEHOLDER } from "@/lib/format";

/** A decimal string from the API ("4012.5000"), shown with Indian grouping and two decimals. */
export function Decimal({ value }: Readonly<{ value: string | null | undefined }>) {
  return value == null ? PLACEHOLDER : formatNumber(Number(value), 2);
}

/** A ratio string from the API ("0.25"), shown as a percent ("25.00%"). */
export function Percent({ value }: Readonly<{ value: string }>) {
  return `${formatNumber(Number(value) * 100, 2)}%`;
}
