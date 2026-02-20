"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

export interface CheckboxProps
    extends Omit<React.InputHTMLAttributes<HTMLInputElement>, "type"> {
    label?: string;
}

const Checkbox = React.forwardRef<HTMLInputElement, CheckboxProps>(
    ({ className, label, id: providedId, ...props }, ref) => {
        const generatedId = React.useId();
        const id = providedId || generatedId;

        return (
            <div className="flex items-center gap-2 group cursor-pointer">
                <div className="relative flex items-center justify-center">
                    <input
                        type="checkbox"
                        ref={ref}
                        id={id}
                        className={cn(
                            "peer h-4 w-4 cursor-pointer appearance-none rounded-sm border border-border bg-background-tertiary transition-all checked:bg-primary checked:border-primary hover:border-primary/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/20",
                            className
                        )}
                        {...props}
                    />
                    <svg
                        aria-hidden="true"
                        className="pointer-events-none absolute h-3 w-3 text-primary-fg opacity-0 transition-opacity peer-checked:opacity-100"
                        xmlns="http://www.w3.org/2000/svg"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="4"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                    >
                        <polyline points="20 6 9 17 4 12" />
                    </svg>
                </div>
                {label && (
                    <label
                        htmlFor={id}
                        className="text-xs font-medium text-foreground-secondary cursor-pointer select-none group-hover:text-foreground transition-colors"
                    >
                        {label}
                    </label>
                )}
            </div>
        );
    }
);

Checkbox.displayName = "Checkbox";

export { Checkbox };
