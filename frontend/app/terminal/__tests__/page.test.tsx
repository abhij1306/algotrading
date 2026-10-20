import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vite-plus/test";
import TerminalPage from "../page";

vi.mock("@/hooks/useWebSocket", () => ({
  useWebSocket: () => ({
    isConnected: false,
    lastMessage: null,
    sendMessage: vi.fn(),
    registerCallback: () => () => undefined,
  }),
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
      <MemoryRouter>
        <TerminalPage />
      </MemoryRouter>,
    );

    expect(screen.getByText("Mode: PAPER")).toBeInTheDocument();
    expect(screen.queryByText("Mode: LIVE")).not.toBeInTheDocument();
    expect(screen.getByLabelText(/order type/i)).toHaveValue("LIMIT");
  });
});
