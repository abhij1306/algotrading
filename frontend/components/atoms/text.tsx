import type { HTMLAttributes } from "react";
import { cn } from "@/lib/utils";

const TONE = {
  default: "",
  muted: "text-foreground-muted",
  loss: "text-loss",
  warning: "text-warning",
} as const;

/** A line of secondary text in one of the page's tones. */
export function Text({
  tone = "default",
  className,
  ...props
}: Readonly<HTMLAttributes<HTMLParagraphElement> & { tone?: keyof typeof TONE }>) {
  return <p className={cn("text-sm", TONE[tone], className)} {...props} />;
}
