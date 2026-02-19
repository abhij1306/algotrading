import * as React from "react";
import { cn } from "@/lib/utils";

export interface SelectProps extends React.SelectHTMLAttributes<HTMLSelectElement> {
  label?: string;
  error?: string;
}

const Select = React.forwardRef<HTMLSelectElement, SelectProps>((props, ref) => {
  const { className, label, error, id: providedId, children, ...rest } = props;
  const generatedId = React.useId();
  const id = providedId || generatedId;

  const selectClass = cn(
    "h-9 w-full rounded-md border border-border bg-background-tertiary px-3 text-sm font-medium text-foreground transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/20 disabled:cursor-not-allowed disabled:opacity-50",
    error && "border-loss focus-visible:ring-loss/20",
    className
  );

  if (!label && !error) {
    return (
      <select ref={ref} id={id} className={selectClass} {...rest}>
        {children}
      </select>
    );
  }

  return (
    <div className="w-full space-y-1">
      {label && (
        <label htmlFor={id} className="block text-xs font-medium text-foreground-secondary">
          {label}
        </label>
      )}
      <select ref={ref} id={id} className={selectClass} {...rest}>
        {children}
      </select>
      {error && <p className="mt-1 text-xs font-medium text-loss">{error}</p>}
    </div>
  );
});

Select.displayName = "Select";

export { Select };
