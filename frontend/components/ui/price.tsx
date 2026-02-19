import * as React from "react";
import { cn, formatPrice, formatPercent, formatCompact } from "@/lib/utils";

/**
 * Price Display Component
 *
 * Specialized component for displaying financial prices with
 * proper formatting, color coding, and optional change indicators.
 */

interface PriceProps extends React.HTMLAttributes<HTMLSpanElement> {
  value: number;
  format?: "default" | "compact" | "currency";
  currency?: string;
  precision?: number;
  showSign?: boolean;
  size?: "sm" | "default" | "lg" | "xl";
}

const Price = React.forwardRef<HTMLSpanElement, PriceProps>(
  (
    {
      value,
      format = "default",
      currency = "$",
      showSign = false,
      size = "default",
      className,
      ...props
    },
    ref
  ) => {
    const sizes = {
      sm: "text-xs",
      default: "text-sm",
      lg: "text-lg",
      xl: "text-2xl font-semibold",
    };

    const formatValue = () => {
      // Handle undefined/null values
      if (value === undefined || value === null || !Number.isFinite(value)) {
        return "-";
      }
      if (format === "compact") {
        return formatCompact(value);
      }
      if (format === "currency") {
        return `${currency}${formatPrice(value)}`;
      }
      return formatPrice(value);
    };

    return (
      <span ref={ref} className={cn("font-mono tabular-nums", sizes[size], className)} {...props}>
        {showSign && value !== undefined && value !== null && value >= 0 && "+"}
        {formatValue()}
      </span>
    );
  }
);
Price.displayName = "Price";

// Price with Change
interface PriceChangeProps extends React.HTMLAttributes<HTMLDivElement> {
  value: number;
  change: number;
  changePercent?: number;
  showPercent?: boolean;
  size?: "sm" | "default" | "lg";
}

const PriceChange = React.forwardRef<HTMLDivElement, PriceChangeProps>(
  (
    { value, change, changePercent, showPercent = true, size = "default", className, ...props },
    ref
  ) => {
    // Handle undefined/null values
    const safeChange = change ?? 0;
    const safeValue = value ?? 0;
    const isPositive = safeChange >= 0;
    const colorClass = isPositive ? "text-profit" : "text-loss";

    return (
      <div ref={ref} className={cn("flex items-center gap-2", className)} {...props}>
        <Price value={safeValue} size={size} />
        <div className={cn("flex items-center text-xs", colorClass)}>
          <span>{isPositive ? "▲" : "▼"}</span>
          <span>{formatPrice(Math.abs(safeChange))}</span>
          {showPercent && changePercent !== undefined && (
            <span className="ml-1 opacity-80">({formatPercent(changePercent)})</span>
          )}
        </div>
      </div>
    );
  }
);
PriceChange.displayName = "PriceChange";

export { Price, PriceChange };
