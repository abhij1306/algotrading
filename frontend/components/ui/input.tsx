"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

export interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  error?: string;
}

const Input = React.forwardRef<HTMLInputElement, InputProps>((props, ref) => {
  const { className, label, error, id: providedId, ...rest } = props;
  const generatedId = React.useId();
  const id = providedId || generatedId;
  const inputClass = cn(
    "h-9 w-full rounded-md border border-border bg-background-tertiary px-3 py-2 text-sm font-medium text-foreground transition-colors placeholder:text-foreground-muted/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/20 disabled:cursor-not-allowed disabled:opacity-50",
    error && "border-loss focus-visible:ring-loss/20",
    className
  );

  if (!label && !error) {
    return <input ref={ref} id={id} className={inputClass} {...rest} />;
  }

  return (
    <div className="w-full space-y-1">
      {label && (
        <label
          htmlFor={id}
          className="block text-xs font-medium text-foreground-secondary"
        >
          {label}
        </label>
      )}
      <input ref={ref} id={id} className={inputClass} {...rest} />
      {error && (
        <p className="mt-1 text-xs font-medium text-loss">
          {error}
        </p>
      )}
    </div>
  );
});

Input.displayName = "Input";

export { Input };
