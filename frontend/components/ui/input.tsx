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

  return (
    <div className="space-y-1.5 w-full">
      {label && (
        <label
          htmlFor={id}
          className="block text-xxs font-black text-foreground-tertiary uppercase tracking-wider ml-1"
        >
          {label}
        </label>
      )}
      <input
        ref={ref}
        id={id}
        className={cn(
          "flex h-12 w-full rounded-xl bg-elevated px-4 py-3 text-sm font-semibold text-foreground transition-all placeholder:text-foreground-muted/50 focus-visible:outline-none focus-visible:bg-elevated focus-visible:ring-2 focus-visible:ring-primary/10 disabled:cursor-not-allowed disabled:opacity-50 border border-border shadow-sm",
          error && "bg-loss-bg/10 focus-visible:ring-loss/20 border-loss",
          className
        )}
        {...rest}
      />
      {error && (
        <p className="text-xxs font-semibold text-loss mt-1 ml-1 uppercase tracking-wider">
          {error}
        </p>
      )}
    </div>
  );
});

Input.displayName = "Input";

export { Input };
