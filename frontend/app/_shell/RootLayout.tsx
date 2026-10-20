import { Outlet } from "react-router";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { AppShell } from "@/components/layout";

export default function RootLayout() {
  return (
    <ErrorBoundary>
      <AppShell>
        <Outlet />
      </AppShell>
    </ErrorBoundary>
  );
}
