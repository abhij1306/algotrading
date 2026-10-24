import { cn } from "@/lib/utils";

/** A small round dot; `spinning` turns it into a ring spinner. */
export function StatusDot({
  className,
  spinning = false,
}: Readonly<{ className?: string; spinning?: boolean }>) {
  return (
    <div
      className={cn(
        "rounded-full",
        spinning && "border-2 border-primary border-t-transparent animate-spin",
        className,
      )}
    />
  );
}
