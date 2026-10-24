import { Outlet } from "react-router";
import { AppShell } from "@/components/templates/app-shell";
import { ErrorBoundary } from "@/components/templates/error-boundary";

export default function RootLayout() {
  return (
    <ErrorBoundary>
      <AppShell>
        <Outlet />
      </AppShell>
    </ErrorBoundary>
  );
}
