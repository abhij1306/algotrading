/** A hand-driven stand-in for the browser WebSocket; install with `vi.stubGlobal("WebSocket", FakeSocket)`. */
export class FakeSocket {
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

  receive(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) });
  }

  deliverClose(code?: number) {
    this.onclose?.({ code: code ?? this.pendingCloseCode ?? 1006 });
  }

  actions(): Array<{ action: string; symbols: string[] }> {
    return this.sent.map((raw) => JSON.parse(raw));
  }
}
