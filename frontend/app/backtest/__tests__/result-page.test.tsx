import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import { createQueryClient } from "@/lib/queries/query-client";

// The client reads VITE_API_URL at import time; Node cannot build a Request from a relative URL.
vi.stubEnv("VITE_API_URL", "http://api.test");
const { default: BacktestResultPage } = await import("../results/[runId]/page");

const PARAMS = {
  instrument_type: "equity",
  start_date: "2025-01-01",
  end_date: "2025-12-31",
  selection: { mode: "universe", universe: "NIFTY50" },
};

const COMPLETED_JOB = {
  job_id: "run-1",
  status: "completed",
  created_at: "2026-10-20T09:15:00",
  params: PARAMS,
  result: {
    selection: { scope: "NIFTY50" },
    metrics: {
      final_equity: 1_250_000,
      total_return_pct: 25.04,
      sharpe_ratio: 1.456,
      max_drawdown_pct: -8.25,
      total_trades: 2,
      win_rate_pct: 50,
    },
    equity_curve: [],
    benchmark_curve: [],
    drawdown_curve: [],
    trade_log: [
      {
        symbol: "TCS",
        entry_date: "2025-02-01",
        exit_date: "2025-03-01",
        entry_price: 3890,
        exit_price: 4012.5,
        return_pct: 3.149,
      },
      {
        symbol: "SBIN",
        entry_date: "2025-04-01",
        exit_date: "2025-05-01",
        entry_price: 812.5,
        exit_price: 790,
        return_pct: -2.769,
      },
    ],
  },
};

function stubJob(job: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(Response.json(job))),
  );
}

function renderResult() {
  const client = createQueryClient();
  client.setDefaultOptions({ queries: { retry: false } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/backtest/results/run-1"]}>
        <Routes>
          <Route path="/backtest/results/:runId" element={<BacktestResultPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("backtest result page", { timeout: 15_000 }, () => {
  it("shows the headline numbers, run summary and trade log of a finished run", async () => {
    stubJob(COMPLETED_JOB);
    renderResult();

    expect(await screen.findByText("25.0%")).toBeInTheDocument();
    expect(screen.getByText("₹12.50L")).toBeInTheDocument();
    expect(screen.getByText("1.46")).toBeInTheDocument();
    expect(screen.getByText("-8.3%")).toBeInTheDocument();
    expect(screen.getByText("50.0%")).toBeInTheDocument();
    expect(screen.getByText("50.0% win rate · by date")).toBeInTheDocument();
    expect(screen.getByText("EQUITY")).toBeInTheDocument();
    expect(screen.getByText("2 trades")).toBeInTheDocument();

    const tcs = screen.getByText("TCS").closest("tr");
    if (tcs === null) throw new Error("TCS is not in a table row");
    const row = within(tcs);
    expect(row.getByText("3,890.00")).toBeInTheDocument();
    expect(row.getByText("4,012.50")).toBeInTheDocument();
    expect(row.getByText("+3.15%")).toBeInTheDocument();
    expect(screen.getByText("-2.77%")).toBeInTheDocument();
  });

  it("shows the failure reason of a failed run", async () => {
    stubJob({
      job_id: "run-1",
      status: "failed",
      created_at: "2026-10-20T09:15:00",
      error: "No data for NIFTY50",
      params: PARAMS,
    });
    renderResult();

    expect(await screen.findByText("No data for NIFTY50")).toBeInTheDocument();
    expect(screen.queryByText("Trade Log v2")).not.toBeInTheDocument();
  });
});
