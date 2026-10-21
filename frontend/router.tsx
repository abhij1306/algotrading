import { lazy, Suspense, type ComponentType, type ReactNode } from "react";
import { createBrowserRouter, Navigate } from "react-router";
import RootLayout from "@/app/_shell/RootLayout";
import RouteError from "@/app/_shell/RouteError";
import NotFound from "@/app/_shell/NotFound";

function page(load: () => Promise<{ default: ComponentType }>, fallback: ReactNode = null) {
  const Page = lazy(load);
  return (
    <Suspense fallback={fallback}>
      <Page />
    </Suspense>
  );
}

export const router = createBrowserRouter([
  {
    element: <RootLayout />,
    errorElement: <RouteError />,
    children: [
      { index: true, element: <Navigate to="/dashboard" replace /> },
      { path: "dashboard", element: page(() => import("@/app/dashboard/page")) },
      { path: "*", element: <NotFound /> },
    ],
  },
]);
