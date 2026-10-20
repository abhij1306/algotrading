/**
 * Module-level store owning the single market-data WebSocket.
 *
 * - The socket connects when the first consumer retains it and closes when the last releases.
 *   Release is deferred one task so a StrictMode mount/unmount/mount never reconnects.
 * - Symbol subscriptions are ref-counted; the server only sees aggregate deltas, and the
 *   desired set is replayed after every (re)connect.
 * - React reads it through `useSyncExternalStore` (see `./hooks.ts`); there are no effects.
 */
import { isObject } from "@/lib/type-guards";

export type ConnectionStatus = "disconnected" | "connecting" | "connected";

export type Quote = {
  readonly ltp?: number;
  readonly changePct?: number;
  readonly volume?: number;
  /** Epoch ms when the last tick for the symbol was applied, changed or not. */
  readonly receivedAt?: number;
};

export type QuoteMap = Readonly<Record<string, Quote>>;

type SocketMessage = {
  type?: string;
  data?: unknown;
  [key: string]: unknown;
};

const MAX_RETRIES = 10;
const BASE_BACKOFF_MS = 2000;
const MAX_BACKOFF_MS = 30000;
const HEARTBEAT_INTERVAL_MS = 30000;
const MAX_HIDDEN_QUEUE_SIZE = 1000;
const PONG_EVENT = '{"type":"pong"}';
const PING_EVENT = "ping";
const PONG_PAYLOAD = JSON.stringify({ type: "pong" });

export function getWebSocketUrl(): string {
  if (import.meta.env.VITE_WS_URL) return import.meta.env.VITE_WS_URL;

  const apiUrl = import.meta.env.VITE_API_URL;
  if (apiUrl && /^https?:\/\//.test(apiUrl)) {
    const u = new URL(apiUrl);
    u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
    return `${u.origin}/api/websocket/stream`;
  }

  // Same origin: the Vite dev/preview proxy forwards the socket to the backend.
  const { protocol, host } = globalThis.location;
  return `${protocol === "https:" ? "wss:" : "ws:"}//${host}/api/websocket/stream`;
}

function numberOrUndefined(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

/** Normalise the provider's field aliases into a `Quote`; null when there is no symbol. */
function toQuoteUpdate(value: unknown): { symbol: string; quote: Quote } | null {
  if (!isObject(value) || typeof value.symbol !== "string" || value.symbol === "") return null;
  return {
    symbol: value.symbol,
    quote: {
      ltp: numberOrUndefined(value.ltp ?? value.lp),
      changePct: numberOrUndefined(value.change_pct ?? value.chp ?? value.changePercent),
      volume: numberOrUndefined(value.volume ?? value.v ?? value.vol_traded_today),
    },
  };
}

/** Expand a raw message into ticker updates (empty for non-ticker messages). */
function extractTicks(message: SocketMessage): unknown[] {
  if (message.type === "ticker_batch" && Array.isArray(message.data)) return message.data;
  if (message.type === "ticker" && message.data) return [message.data];
  return [];
}

function parseMessage(raw: string): SocketMessage | null {
  const parsed: unknown = JSON.parse(raw);
  return isObject(parsed) ? parsed : null;
}

export function createQuoteStore() {
  let status: ConnectionStatus = "disconnected";
  let quotes: QuoteMap = {};

  let socket: WebSocket | null = null;
  let retainCount = 0;
  let retryCount = 0;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  let releaseTimer: ReturnType<typeof setTimeout> | null = null;
  let syncScheduled = false;
  let hiddenQueue: SocketMessage[] = [];

  const symbolCounts = new Map<string, number>();
  const serverSubscribed = new Set<string>();
  const listeners = new Set<() => void>();

  function notify() {
    for (const listener of listeners) listener();
  }

  function setStatus(next: ConnectionStatus) {
    if (status === next) return;
    status = next;
    notify();
  }

  function send(message: unknown): boolean {
    if (socket?.readyState !== WebSocket.OPEN) {
      console.warn("[WebSocket] Cannot send: Not connected");
      return false;
    }
    socket.send(typeof message === "string" ? message : JSON.stringify(message));
    return true;
  }

  /** Reconcile the server's subscription set with the desired set by sending only the delta. */
  function syncSubscriptions() {
    syncScheduled = false;
    if (socket?.readyState !== WebSocket.OPEN) return;
    const desired = new Set(symbolCounts.keys());
    const toAdd = [...desired].filter((symbol) => !serverSubscribed.has(symbol));
    const toRemove = [...serverSubscribed].filter((symbol) => !desired.has(symbol));
    if (toRemove.length > 0) {
      socket.send(JSON.stringify({ action: "unsubscribe", symbols: toRemove }));
      for (const symbol of toRemove) serverSubscribed.delete(symbol);
    }
    if (toAdd.length > 0) {
      socket.send(JSON.stringify({ action: "subscribe", symbols: toAdd }));
      for (const symbol of toAdd) serverSubscribed.add(symbol);
    }
  }

  function scheduleSync() {
    if (syncScheduled) return;
    syncScheduled = true;
    queueMicrotask(syncSubscriptions);
  }

  function applyMessage(message: SocketMessage) {
    let nextQuotes: Record<string, Quote> | null = null;
    const receivedAt = Date.now();
    for (const tick of extractTicks(message)) {
      const update = toQuoteUpdate(tick);
      if (!update) continue;
      const previous = (nextQuotes ?? quotes)[update.symbol];
      const merged: Quote = {
        ltp: update.quote.ltp ?? previous?.ltp,
        changePct: update.quote.changePct ?? previous?.changePct,
        volume: update.quote.volume ?? previous?.volume,
        // A repeat tick still proves the stream is alive, so it refreshes the timestamp.
        receivedAt,
      };
      nextQuotes ??= { ...quotes };
      nextQuotes[update.symbol] = merged;
    }
    if (!nextQuotes) return;
    quotes = nextQuotes;
    notify();
  }

  function flushHiddenQueue() {
    if (document.hidden || hiddenQueue.length === 0) return;
    const queued = hiddenQueue;
    hiddenQueue = [];
    for (const message of queued) applyMessage(message);
  }

  function stopHeartbeat() {
    if (heartbeatTimer) clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }

  function scheduleReconnect() {
    if (retainCount === 0 || reconnectTimer) return;
    if (retryCount >= MAX_RETRIES) {
      console.error("[WebSocket] Max reconnect attempts reached");
      return;
    }
    const delay = Math.min(BASE_BACKOFF_MS * 2 ** retryCount, MAX_BACKOFF_MS);
    retryCount += 1;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, delay);
  }

  function connect() {
    if (socket || retainCount === 0) return;
    setStatus("connecting");

    let next: WebSocket;
    try {
      next = new WebSocket(getWebSocketUrl());
    } catch (error) {
      console.warn("[WebSocket] Connection creation failed:", error);
      setStatus("disconnected");
      scheduleReconnect();
      return;
    }
    socket = next;

    next.onopen = () => {
      if (socket !== next) return;
      retryCount = 0;
      setStatus("connected");
      stopHeartbeat();
      heartbeatTimer = setInterval(() => {
        if (next.readyState === WebSocket.OPEN) next.send(PING_EVENT);
      }, HEARTBEAT_INTERVAL_MS);
      syncSubscriptions();
    };

    next.onmessage = (event) => {
      if (socket !== next || typeof event.data !== "string") return;
      if (event.data === PONG_EVENT) return;
      if (event.data === PING_EVENT) {
        next.send(PONG_PAYLOAD);
        return;
      }
      try {
        const message = parseMessage(event.data);
        if (!message) return;
        if (document.hidden) {
          hiddenQueue.push(message);
          if (hiddenQueue.length > MAX_HIDDEN_QUEUE_SIZE) {
            hiddenQueue.splice(0, hiddenQueue.length - MAX_HIDDEN_QUEUE_SIZE);
          }
          return;
        }
        applyMessage(message);
      } catch (error) {
        console.warn("[WebSocket] Message parse error:", error);
      }
    };

    next.onclose = () => {
      // A superseded socket closes after its replacement is installed; it must not
      // touch the newer socket's state.
      if (socket !== next) return;
      socket = null;
      stopHeartbeat();
      serverSubscribed.clear();
      setStatus("disconnected");
      scheduleReconnect();
    };

    next.onerror = (event) => {
      console.warn("[WebSocket] Connection error:", { type: event.type, url: next.url });
    };
  }

  function disconnect() {
    releaseTimer = null;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = null;
    stopHeartbeat();
    hiddenQueue = [];
    serverSubscribed.clear();
    retryCount = 0;
    document.removeEventListener("visibilitychange", flushHiddenQueue);
    if (socket) {
      const closing = socket;
      socket = null;
      closing.close(1000, "No more subscribers");
    }
    setStatus("disconnected");
  }

  /** Keep the connection open until the returned function is called. */
  function retain(): () => void {
    retainCount += 1;
    if (releaseTimer) clearTimeout(releaseTimer);
    releaseTimer = null;
    if (retainCount === 1) {
      document.addEventListener("visibilitychange", flushHiddenQueue);
      connect();
    }
    let released = false;
    return () => {
      if (released) return;
      released = true;
      retainCount -= 1;
      if (retainCount === 0) releaseTimer = setTimeout(disconnect, 0);
    };
  }

  /** Subscribe to symbols (ref-counted across consumers). */
  function subscribeSymbols(symbols: readonly string[]): () => void {
    const unique = [...new Set(symbols)];
    for (const symbol of unique) symbolCounts.set(symbol, (symbolCounts.get(symbol) ?? 0) + 1);
    scheduleSync();
    let released = false;
    return () => {
      if (released) return;
      released = true;
      for (const symbol of unique) {
        const count = (symbolCounts.get(symbol) ?? 1) - 1;
        if (count <= 0) symbolCounts.delete(symbol);
        else symbolCounts.set(symbol, count);
      }
      scheduleSync();
    };
  }

  function subscribe(listener: () => void): () => void {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }

  return {
    retain,
    subscribe,
    subscribeSymbols,
    send,
    getStatus: () => status,
    getQuotes: () => quotes,
    getQuote: (symbol: string): Quote | undefined => quotes[symbol],
  };
}

export type QuoteStore = ReturnType<typeof createQuoteStore>;

export const quoteStore: QuoteStore = createQuoteStore();
