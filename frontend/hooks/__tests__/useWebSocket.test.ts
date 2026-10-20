import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vite-plus/test";
import { useWebSocket } from "../useWebSocket";

class FakeSocket {
  static instances: FakeSocket[] = [];
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  readyState = FakeSocket.CONNECTING;
  sent: string[] = [];
  pendingCloseCode: number | null = null;
  url: string;
  onopen: (() => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close(code = 1000) {
    this.readyState = FakeSocket.CLOSED;
    // Real sockets deliver close asynchronously; tests fire it by hand.
    this.pendingCloseCode = code;
  }

  open() {
    this.readyState = FakeSocket.OPEN;
    this.onopen?.();
  }

  deliverClose() {
    this.onclose?.({ code: this.pendingCloseCode ?? 1006 });
  }
}

describe("useWebSocket", () => {
  beforeEach(() => {
    FakeSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeSocket);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("keeps the newer connection when a superseded socket closes late", () => {
    const { result, rerender } = renderHook(({ enabled }) => useWebSocket({ enabled }), {
      initialProps: { enabled: true },
    });
    const first = FakeSocket.instances[0];
    act(() => first.open());
    expect(result.current.isConnected).toBe(true);

    rerender({ enabled: false });
    expect(result.current.isConnected).toBe(false);
    rerender({ enabled: true });
    const second = FakeSocket.instances[1];
    act(() => second.open());
    expect(result.current.isConnected).toBe(true);

    // The first socket's close event arrives after the second one is live.
    act(() => first.deliverClose());

    expect(result.current.isConnected).toBe(true);
    act(() => result.current.sendMessage("hello"));
    expect(second.sent).toContain("hello");
  });
});
