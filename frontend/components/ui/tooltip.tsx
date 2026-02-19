import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Tooltip Component
 *
 * Simple tooltip for displaying additional information.
 */

interface TooltipProps {
  tooltipContent: React.ReactNode;
  side?: "top" | "bottom" | "left" | "right";
  delay?: number;
  className?: string;
  children: React.ReactNode;
}

const Tooltip = React.forwardRef<HTMLDivElement, TooltipProps>(
  ({ tooltipContent, side = "top", delay = 200, className, children }, ref) => {
    const [isVisible, setIsVisible] = React.useState(false);
    const timeoutRef = React.useRef<NodeJS.Timeout | null>(null);

    const showTooltip = () => {
      timeoutRef.current = setTimeout(() => setIsVisible(true), delay);
    };

    const hideTooltip = () => {
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current);
      }
      setIsVisible(false);
    };

    const sideStyles = {
      top: "bottom-full left-1/2 -translate-x-1/2 mb-2",
      bottom: "top-full left-1/2 -translate-x-1/2 mt-2",
      left: "right-full top-1/2 -translate-y-1/2 mr-2",
      right: "left-full top-1/2 -translate-y-1/2 ml-2",
    };

    return (
      <div
        ref={ref}
        className="relative inline-flex"
        onMouseEnter={showTooltip}
        onMouseLeave={hideTooltip}
      >
        {children}
        {isVisible && (
          <div
            className={cn(
              "absolute z-tooltip px-2 py-1 text-xs rounded-md",
              "bg-elevated border border-border shadow-lg",
              "text-foreground-muted whitespace-nowrap",
              "animate-in fade-in zoom-in-95 duration-200",
              sideStyles[side],
              className
            )}
          >
            {tooltipContent}
          </div>
        )}
      </div>
    );
  }
);
Tooltip.displayName = "Tooltip";

export { Tooltip };
