import { lazy, Suspense, type ComponentType, type ReactNode } from "react";
import { createBrowserRouter, Navigate } from "react-router";
import RootLayout from "@/app/_shell/RootLayout";
import RouteError from "@/app/_shell/RouteError";
import NotFound from "@/app/_shell/NotFound";
import BacktestLayout from "@/app/backtest/layout";
import ScreenerLoading from "@/app/screener/loading";

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
      { path: "screener", element: page(() => import("@/app/screener/page"), <ScreenerLoading />) },
      { path: "terminal", element: page(() => import("@/app/terminal/page")) },
      { path: "strategies", element: page(() => import("@/app/strategies/page")) },
      {
        path: "backtest",
        element: <BacktestLayout />,
        children: [
          { index: true, element: page(() => import("@/app/backtest/page")) },
          { path: "new", element: page(() => import("@/app/backtest/new/page")) },
          { path: "results/:runId", element: page(() => import("@/app/backtest/results/[runId]/page")) },
        ],
      },
      { path: "*", element: <NotFound /> },
    ],
  },
]);
