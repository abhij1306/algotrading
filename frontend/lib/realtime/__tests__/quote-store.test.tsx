import { act, render, renderHook, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vite-plus/test";
import { useConnectionStatus, useQuote, useQuotes } from "../hooks";
import { createQuoteStore } from "../quote-store";
import { FakeSocket } from "./fake-socket";

async function flush() {
  // Microtask (subscription sync) then the deferred-release timer.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1);
  });
}

describe("quote store", () => {
  beforeEach(() => {
    FakeSocket.instances = [];
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", FakeSocket);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("opens one socket for many consumers and closes after the last unsubscribes", async () => {
    const store = createQuoteStore();
    const a = renderHook(() => useConnectionStatus({ store }));
    const b = renderHook(() => useConnectionStatus({ store }));
    expect(FakeSocket.instances).toHaveLength(1);

    act(() => FakeSocket.instances[0].open());
    expect(a.result.current).toBe("connected");

    a.unmount();
    await flush();
    expect(b.result.current).toBe("connected");

    b.unmount();
    await flush();
    expect(store.getStatus()).toBe("disconnected");
    expect(FakeSocket.instances[0].readyState).toBe(FakeSocket.CLOSED);
  });

  it("holds no socket and reads disconnected while disabled", () => {
    const store = createQuoteStore();
    const connected = renderHook(() => useConnectionStatus({ store }));
    act(() => FakeSocket.instances[0].open());

    const disabled = renderHook(() => useConnectionStatus({ store, enabled: false }));
    expect(disabled.result.current).toBe("disconnected");
    expect(FakeSocket.instances).toHaveLength(1);

    connected.unmount();
  });

  it("does not double-connect under StrictMode", async () => {
    const store = createQuoteStore();
    renderHook(() => useConnectionStatus({ store }), { wrapper: StrictMode });
    await flush();
    expect(FakeSocket.instances).toHaveLength(1);
    expect(FakeSocket.instances[0].readyState).not.toBe(FakeSocket.CLOSED);
  });

  it("keeps the newer connection when a superseded socket closes late", async () => {
    const store = createQuoteStore();
    const first = renderHook(() => useConnectionStatus({ store }));
    const firstSocket = FakeSocket.instances[0];
    act(() => firstSocket.open());
    first.unmount();
    await flush();

    const second = renderHook(() => useConnectionStatus({ store }));
    const secondSocket = FakeSocket.instances[1];
    act(() => secondSocket.open());
    expect(second.result.current).toBe("connected");

    act(() => firstSocket.deliverClose());

    expect(second.result.current).toBe("connected");
    act(() => {
      store.send("hello");
    });
    expect(secondSocket.sent).toContain("hello");
  });

  it("reconnects with backoff and replays subscriptions", async () => {
    const store = createQuoteStore();
    renderHook(() => useQuotes(["SBIN", "TCS"], store));
    const first = FakeSocket.instances[0];
    act(() => first.open());
    await flush();
    expect(first.actions()).toEqual([{ action: "subscribe", symbols: ["SBIN", "TCS"] }]);

    act(() => first.deliverClose(1006));
    expect(store.getStatus()).toBe("disconnected");
    expect(FakeSocket.instances).toHaveLength(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(FakeSocket.instances).toHaveLength(2);
    act(() => FakeSocket.instances[1].open());
    expect(FakeSocket.instances[1].actions()).toEqual([
      { action: "subscribe", symbols: ["SBIN", "TCS"] },
    ]);
  });

  it("sends only aggregate subscription deltas across consumers", async () => {
    const store = createQuoteStore();
    const one = renderHook(() => useQuotes(["SBIN"], store));
    act(() => FakeSocket.instances[0].open());
    const socket = FakeSocket.instances[0];
    await flush();

    const two = renderHook(() => useQuotes(["SBIN", "TCS"], store));
    await flush();
    one.unmount();
    await flush();
    two.unmount();
    await flush();

    expect(socket.actions()).toEqual([
      { action: "subscribe", symbols: ["SBIN"] },
      { action: "subscribe", symbols: ["TCS"] },
      { action: "unsubscribe", symbols: ["SBIN", "TCS"] },
    ]);
  });

  it("normalises ticks and re-renders only the subscribed symbol's consumer", async () => {
    const store = createQuoteStore();
    const onSbinRender = vi.fn();
    function Sbin() {
      onSbinRender();
      const quote = useQuote("SBIN", store);
      return <span data-testid="sbin">{quote?.ltp ?? "none"}</span>;
    }
    render(<Sbin />);
    act(() => FakeSocket.instances[0].open());
    expect(screen.getByTestId("sbin")).toHaveTextContent("none");

    act(() =>
      FakeSocket.instances[0].receive({
        type: "ticker_batch",
        data: [
          { symbol: "SBIN", lp: 812.5, chp: 1.2 },
          { symbol: "TCS", ltp: 4000 },
        ],
      }),
    );
    expect(screen.getByTestId("sbin")).toHaveTextContent("812.5");
    expect(store.getQuote("SBIN")).toEqual({
      ltp: 812.5,
      changePct: 1.2,
      volume: undefined,
      receivedAt: Date.now(),
    });

    const rendersBefore = onSbinRender.mock.calls.length;
    act(() =>
      FakeSocket.instances[0].receive({ type: "ticker", data: { symbol: "TCS", ltp: 4001 } }),
    );
    expect(onSbinRender).toHaveBeenCalledTimes(rendersBefore);
  });
});
