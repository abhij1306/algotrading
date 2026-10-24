import type { ReactNode } from "react";

/** A page body in the shell's gutter; with `title`, a heading, its `description` and an `aside`. */
export function PageLayout({
  title,
  description,
  aside,
  children,
}: Readonly<{ title?: string; description?: string; aside?: ReactNode; children: ReactNode }>) {
  return (
    <div className="mx-auto w-full max-w-[1440px] px-4 pt-8 pb-16 md:px-10 md:pt-12">
      {title ? (
        <div className="mb-10 flex flex-wrap items-end justify-between gap-x-8 gap-y-2 md:mb-14">
          <div>
            <h1 className="text-xl">{title}</h1>
            {description ? (
              <p className="mt-1 text-sm text-foreground-muted">{description}</p>
            ) : null}
          </div>
          {aside}
        </div>
      ) : null}
      {children}
    </div>
  );
}
