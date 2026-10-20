import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vite-plus/test";
import { createQueryClient } from "@/lib/queries/query-client";
import { TerminalScreen as TerminalPage } from "../TerminalScreen";

vi.mock("@/lib/realtime/hooks", () => ({
  useConnectionStatus: () => "disconnected",
  useQuotes: () => ({}),
}));

describe("terminal order defaults", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response("[]", { status: 200 }))),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    globalThis.localStorage.clear();
  });

  it("starts in PAPER with a LIMIT order type even if LIVE was stored earlier", () => {
    globalThis.localStorage.setItem("terminal_trading_mode", "LIVE");
    render(
      <QueryClientProvider client={createQueryClient()}>
        <MemoryRouter>
          <TerminalPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(screen.getByText("Mode: PAPER")).toBeInTheDocument();
    expect(screen.queryByText("Mode: LIVE")).not.toBeInTheDocument();
    expect(screen.getByLabelText(/order type/i)).toHaveValue("LIMIT");
  });
});
