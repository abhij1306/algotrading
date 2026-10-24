import type { ReactNode } from "react";
import { AppHeader } from "@/components/organisms/header";

/** The header over the scrolling page area. */
export function AppShell({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <div className="flex min-h-dvh flex-col bg-background">
      <AppHeader />
      <main className="flex-1 overflow-x-hidden">{children}</main>
    </div>
  );
}
