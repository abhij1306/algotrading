import { useCallback, useMemo, useSyncExternalStore } from "react";
import {
  quoteStore,
  type ConnectionStatus,
  type Quote,
  type QuoteMap,
  type QuoteStore,
  type SocketMessage,
} from "./quote-store";

/**
 * Hooks over the module-level quote store. Subscribing is the lifecycle: React calls the
 * `subscribe` function on mount and its cleanup on unmount, so the socket connects with the
 * first mounted consumer and closes after the last, with no effects in components.
 */

/** Connection state. Holds the socket open while the calling component is mounted. */
export function useConnectionStatus(store: QuoteStore = quoteStore): ConnectionStatus {
  const subscribe = useCallback(
    (onChange: () => void) => {
      const release = store.retain();
      const unsubscribe = store.subscribe(onChange);
      return () => {
        unsubscribe();
        release();
      };
    },
    [store],
  );
  return useSyncExternalStore(subscribe, store.getStatus);
}

/** Live quote for one symbol; re-renders only when that symbol's quote changes. */
export function useQuote(symbol: string, store: QuoteStore = quoteStore): Quote | undefined {
  const subscribe = useCallback(
    (onChange: () => void) => {
      const release = store.retain();
      const unsubscribe = store.subscribe(onChange);
      const unsubscribeSymbol = symbol === "" ? undefined : store.subscribeSymbols([symbol]);
      return () => {
        unsubscribeSymbol?.();
        unsubscribe();
        release();
      };
    },
    [store, symbol],
  );
  return useSyncExternalStore(subscribe, () => store.getQuote(symbol));
}

const NO_QUOTES: QuoteMap = {};

/** Live quotes for a set of symbols. Pass an empty list to hold no subscriptions. */
export function useQuotes(symbols: readonly string[], store: QuoteStore = quoteStore): QuoteMap {
  const symbolsKey = useMemo(
    () => [...new Set(symbols)].sort((a, b) => a.localeCompare(b)).join(","),
    [symbols],
  );
  const subscribe = useCallback(
    (onChange: () => void) => {
      if (symbolsKey === "") return store.subscribe(onChange);
      const release = store.retain();
      const unsubscribe = store.subscribe(onChange);
      const unsubscribeSymbols = store.subscribeSymbols(symbolsKey.split(","));
      return () => {
        unsubscribeSymbols();
        unsubscribe();
        release();
      };
    },
    [store, symbolsKey],
  );
  const all = useSyncExternalStore(subscribe, store.getQuotes);
  return symbolsKey === "" ? NO_QUOTES : all;
}

/**
 * Every quote the store already holds, without opening the socket or subscribing symbols:
 * shows live numbers only where another consumer is streaming them.
 */
export function useKnownQuotes(store: QuoteStore = quoteStore): QuoteMap {
  return useSyncExternalStore(store.subscribe, store.getQuotes);
}

type WebSocketOptions = {
  /** Do not re-render on every message (consumers that use `registerCallback`). */
  skipStateUpdates?: boolean;
  /** Hold the connection only while true. */
  enabled?: boolean;
};

/**
 * Compatibility surface for components that manage their own subscriptions
 * (screener, dashboard). New code should prefer `useQuote` / `useQuotes`.
 */
export function useWebSocket(options: WebSocketOptions = {}) {
  const { skipStateUpdates = false, enabled = true } = options;
  const store = quoteStore;

  const subscribeStatus = useCallback(
    (onChange: () => void) => {
      const release = enabled ? store.retain() : undefined;
      const unsubscribe = store.subscribe(onChange);
      return () => {
        unsubscribe();
        release?.();
      };
    },
    [store, enabled],
  );
  const status = useSyncExternalStore(subscribeStatus, store.getStatus);
  const lastMessage = useSyncExternalStore(
    store.subscribe,
    skipStateUpdates ? getNoMessage : store.getLastMessage,
  );

  return {
    isConnected: enabled && status === "connected",
    lastMessage,
    sendMessage: store.send,
    registerCallback: store.onMessage,
  };
}

function getNoMessage(): SocketMessage | null {
  return null;
}
