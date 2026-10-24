import { Outlet } from "react-router";
import { AppShell } from "@/components/templates/app-shell";

export default function RootLayout() {
  return (
    <AppShell>
      <Outlet />
    </AppShell>
  );
}
