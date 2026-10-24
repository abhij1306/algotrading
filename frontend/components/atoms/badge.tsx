import type { HTMLAttributes } from "react";
import { cn } from "@/lib/utils";

const TONE = {
  profit: "text-profit",
  loss: "text-loss",
  warning: "text-warning",
  mode: "text-mode",
  muted: "text-foreground-muted",
} as const;

export type Tone = keyof typeof TONE;

/** A state label: a small dot and text in one semantic tone, no box around it. */
export function Badge({
  tone = "muted",
  className,
  children,
  ...props
}: Readonly<HTMLAttributes<HTMLSpanElement> & { tone?: Tone }>) {
  return (
    <span
      className={cn("inline-flex items-center gap-1.5 whitespace-nowrap", TONE[tone], className)}
      {...props}
    >
      <span aria-hidden className="size-1.5 shrink-0 rounded-full bg-current" />
      {children}
    </span>
  );
}
