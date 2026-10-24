import type { ReactNode } from "react";
import { X } from "lucide-react";
import { Button } from "@/components/atoms/button";
import { openModal } from "@/lib/focus";

/** A modal sheet from the right edge for one row's details; Esc, the scrim or ✕ closes it. */
export function Drawer({
  title,
  subtitle,
  onClose,
  children,
}: Readonly<{ title: string; subtitle?: string; onClose: () => void; children: ReactNode }>) {
  return (
    <dialog
      ref={openModal}
      onClose={onClose}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      aria-label={title}
      className="my-0 mr-0 ml-auto h-dvh max-h-none w-full max-w-md bg-background text-foreground shadow-xl"
    >
      <div className="flex min-h-full flex-col gap-6 p-6">
        <header className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-lg">{title}</h2>
            {subtitle ? <p className="text-sm text-foreground-muted">{subtitle}</p> : null}
          </div>
          <Button size="icon" onClick={onClose} aria-label="Close details">
            <X className="size-4" />
          </Button>
        </header>
        {children}
      </div>
    </dialog>
  );
}

/** The label-over-value grid a drawer shows its fields in. */
export function DetailList({ children }: Readonly<{ children: ReactNode }>) {
  return <dl className="grid grid-cols-2 gap-x-6 gap-y-5">{children}</dl>;
}
