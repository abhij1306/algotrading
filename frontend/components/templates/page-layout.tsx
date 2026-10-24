import type { ReactNode } from "react";

/** A scrolling page body; with `title`, a heading and its `description` lead the content. */
export function PageLayout({
  title,
  description,
  children,
}: Readonly<{ title?: string; description?: string; children: ReactNode }>) {
  return (
    <div className="flex flex-col w-full bg-background transition-colors duration-200 min-h-0 flex-1 overflow-auto">
      <div className="flex-1 w-full p-4 md:p-6 space-y-6 max-w-[1920px] mx-auto">
        {title ? <h1 className="text-xl font-semibold text-foreground">{title}</h1> : null}
        {description ? (
          <p className="mt-1 mb-4 text-sm text-foreground-secondary">{description}</p>
        ) : null}
        {children}
      </div>
    </div>
  );
}
