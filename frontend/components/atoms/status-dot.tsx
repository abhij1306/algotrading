import { cn } from "@/lib/utils";

/** A small round dot in the current text colour; `spinning` turns it into a ring spinner. */
export function StatusDot({
  className,
  spinning = false,
}: Readonly<{ className?: string; spinning?: boolean }>) {
  return (
    <span
      aria-hidden
      className={cn(
        "inline-block size-2 shrink-0 rounded-full",
        spinning ? "animate-spin border-2 border-current border-t-transparent" : "bg-current",
        className,
      )}
    />
  );
}
