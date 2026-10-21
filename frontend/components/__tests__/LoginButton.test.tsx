import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import { createQueryClient } from "@/lib/queries/query-client";

// The client reads VITE_API_URL at import time; Node cannot build a Request from a relative URL.
vi.stubEnv("VITE_API_URL", "http://api.test");
const { default: LoginButton } = await import("../LoginButton");

type Route = () => Response | Promise<Response>;

function stubBackend(routes: Record<string, Route>) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL): Promise<Response> => {
      const request = input instanceof Request ? input : new Request(String(input));
      const path = new URL(request.url).pathname;
      calls.push(`${request.method} ${path}`);
      const route = routes[path];
      return Promise.resolve(route ? route() : Response.json({}, { status: 404 }));
    }),
  );
  return calls;
}

function renderButton() {
  render(
    <QueryClientProvider client={createQueryClient()}>
      <LoginButton />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("LoginButton", () => {
  it("shows the connected Fyers user from the status endpoint", async () => {
    const calls = stubBackend({
      "/api/hat/auth/fyers/status": () => Response.json({ connected: true, user_id: "XA1234" }),
    });
    renderButton();

    expect(await screen.findByText("XA1234")).toBeInTheDocument();
    expect(calls).toEqual(["GET /api/hat/auth/fyers/status"]);
  });

  it("reads an unreachable backend as System Offline", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))),
    );
    renderButton();

    expect(await screen.findByText("System Offline")).toBeInTheDocument();
  });

  it("shows the broker outage message when the login URL cannot be built", async () => {
    stubBackend({
      "/api/hat/auth/fyers/status": () => Response.json({ connected: false }),
      "/api/hat/auth/fyers/url": () =>
        Response.json({ error: { code: "BROKER_UNAVAILABLE" } }, { status: 503 }),
    });
    renderButton();

    fireEvent.click(await screen.findByRole("button", { name: /connect fyers/i }));

    expect(
      await screen.findByText(
        "Broker unavailable: Fyers could not be reached. Retry once it is back.",
      ),
    ).toBeInTheDocument();
  });

  it("opens the login page, exchanges the code and re-reads the status", async () => {
    let connected = false;
    const calls = stubBackend({
      "/api/hat/auth/fyers/status": () =>
        Response.json(connected ? { connected: true, user_id: "XA1234" } : { connected: false }),
      "/api/hat/auth/fyers/url": () => Response.json({ url: "https://fyers.test/login" }),
      "/api/hat/auth/fyers/token": () => {
        connected = true;
        return Response.json({ status: "ok" });
      },
    });
    const open = vi.spyOn(window, "open").mockReturnValue(window);
    renderButton();

    fireEvent.click(await screen.findByRole("button", { name: /connect fyers/i }));
    fireEvent.change(await screen.findByLabelText("Authorization Code"), {
      target: { value: "abc123" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Authenticate" }));

    // The re-read status switches the button to the connected badge.
    expect(await screen.findByText("XA1234")).toBeInTheDocument();
    expect(open).toHaveBeenCalledWith("https://fyers.test/login", "_blank", "width=800,height=600");
    await waitFor(() =>
      expect(calls).toEqual([
        "GET /api/hat/auth/fyers/status",
        "GET /api/hat/auth/fyers/url",
        "POST /api/hat/auth/fyers/token",
        "GET /api/hat/auth/fyers/status",
      ]),
    );
  });
});
