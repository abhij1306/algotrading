import * as React from "react";
import { cn, formatPrice, formatPercent } from "@/lib/utils";

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
  ({ 
    value, 
    format = "default", 
    currency = "$",
    precision,
    showSign = false,
    size = "default",
    className,
    ...props 
  }, ref) => {
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
        return value >= 1000000 
          ? `${(value / 1000000).toFixed(1)}M`
          : value >= 1000 
            ? `${(value / 1000).toFixed(1)}K`
            : value.toFixed(2);
      }
      if (format === "currency") {
        return `${currency}${formatPrice(value)}`;
      }
      return formatPrice(value);
    };

    return (
      <span
        ref={ref}
        className={cn(
          "font-mono tabular-nums",
          sizes[size],
          className
        )}
        {...props}
      >
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
  ({ 
    value, 
    change, 
    changePercent,
    showPercent = true,
    size = "default",
    className,
    ...props 
  }, ref) => {
    // Handle undefined/null values
    const safeChange = change ?? 0;
    const safeValue = value ?? 0;
    const isPositive = safeChange >= 0;
    const colorClass = isPositive 
      ? "text-[var(--color-profit)]" 
      : "text-[var(--color-loss)]";

    const sizes = {
      sm: { price: "text-sm", change: "text-xs" },
      default: { price: "text-lg", change: "text-sm" },
      lg: { price: "text-2xl", change: "text-base" },
    };

    return (
      <div ref={ref} className={cn("flex flex-col gap-0.5", className)} {...props}>
        <Price 
          value={safeValue} 
          size={size === "lg" ? "xl" : size === "sm" ? "sm" : "lg"} 
        />
        <div className={cn("flex items-center gap-2", sizes[size].change)}>
          <span className={cn("font-mono tabular-nums", colorClass)}>
            {isPositive ? "+" : ""}{safeChange.toFixed(2)}
          </span>
          {showPercent && changePercent !== undefined && changePercent !== null && (
            <span className={cn("font-mono tabular-nums", colorClass)}>
              ({isPositive ? "+" : ""}{changePercent.toFixed(2)}%)
            </span>
          )}
        </div>
      </div>
    );
  }
);
PriceChange.displayName = "PriceChange";

// Ticker Price (with animation potential)
interface TickerPriceProps extends React.HTMLAttributes<HTMLDivElement> {
  symbol: string;
  price: number;
  change: number;
  changePercent: number;
  volume?: string;
}

const TickerPrice = React.forwardRef<HTMLDivElement, TickerPriceProps>(
  ({ symbol, price, change, changePercent, volume, className, ...props }, ref) => {
    // Handle undefined/null values
    const safePrice = price ?? 0;
    const safeChange = change ?? 0;
    const safeChangePercent = changePercent ?? 0;
    const isPositive = safeChange >= 0;
    const bgClass = isPositive 
      ? "bg-[var(--color-profit-bg)]" 
      : "bg-[var(--color-loss-bg)]";
    const textClass = isPositive 
      ? "text-[var(--color-profit)]" 
      : "text-[var(--color-loss)]";

    return (
      <div
        ref={ref}
        className={cn(
          "flex items-center gap-4 px-3 py-2 rounded-lg",
          bgClass,
          className
        )}
        {...props}
      >
        <span className="font-semibold text-[var(--text-primary)]">{symbol}</span>
        <Price value={safePrice} size="default" />
        <div className={cn("flex items-center gap-1 font-mono tabular-nums text-sm", textClass)}>
          <span>{isPositive ? "▲" : "▼"}</span>
          <span>{Math.abs(safeChangePercent).toFixed(2)}%</span>
        </div>
        {volume && (
          <span className="text-xs text-[var(--text-muted)]">Vol: {volume}</span>
        )}
      </div>
    );
  }
);
TickerPrice.displayName = "TickerPrice";

export { Price, PriceChange, TickerPrice };
