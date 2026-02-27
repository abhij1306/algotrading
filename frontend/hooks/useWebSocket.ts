'use client';

import { useState, useEffect, useCallback, useRef } from 'react';
import { isObject, isTickerData } from '@/lib/type-guards';

interface WebSocketMessage {
  type?: string;
  data?: unknown;
  symbol?: string;
  ltp?: number;
  [key: string]: unknown;
}

interface WebSocketHookReturn {
  isConnected: boolean;
  lastMessage: WebSocketMessage | null;
  sendMessage: (message: unknown) => void;
  registerCallback: (cb: (msg: WebSocketMessage) => void) => () => void;
}

const RECONNECT_INTERVAL = 5000;
const MAX_RETRIES = 10;
const HEARTBEAT_INTERVAL_MS = 30000;
const MAX_HIDDEN_QUEUE_SIZE = 1000;
const PONG_EVENT = '{"type":"pong"}';
const PING_EVENT = 'ping';
const PONG_PAYLOAD = JSON.stringify({ type: 'pong' });

interface WebSocketOptions {
  skipStateUpdates?: boolean;
}

function emitToCallbacks(
  callbacks: Set<(msg: WebSocketMessage) => void>,
  message: WebSocketMessage
): void {
  callbacks.forEach((cb) => {
    try {
      cb(message);
    } catch {
      // Isolate callback errors so one consumer cannot break others.
    }
  });
}

function dispatchIncomingMessage(
  message: WebSocketMessage,
  normalizeTicker: (value: unknown) => Record<string, unknown> | null,
  emit: (msg: WebSocketMessage) => void
): void {
  if (message.type === 'ticker_batch' && Array.isArray(message.data)) {
    for (const tick of message.data) {
      const normalized = normalizeTicker(tick);
      if (normalized) {
        emit({ type: 'ticker', data: normalized });
        continue;
      }
      if (isTickerData(tick)) {
        emit({ type: 'ticker', data: tick });
      }
    }
    return;
  }

  if (message.type === 'ticker' && message.data) {
    const normalized = normalizeTicker(message.data);
    emit(normalized ? { ...message, data: normalized } : message);
    return;
  }

  emit(message);
}

export function useWebSocket(options: WebSocketOptions = {}): WebSocketHookReturn {
  const [isConnected, setIsConnected] = useState(false);
  const [lastMessage, setLastMessage] = useState<WebSocketMessage | null>(null);

  // Callback registry avoiding re-renders
  const callbacksRef = useRef<Set<(msg: WebSocketMessage) => void>>(new Set());

  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const heartbeatIntervalRef = useRef<NodeJS.Timeout | null>(null);
  const isConnectingRef = useRef(false);
  const retryCountRef = useRef(0);
  const cancelledRef = useRef(false);
  const messagesQueueRef = useRef<WebSocketMessage[]>([]);

  // Ref for options to avoid stale closure
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const getWsUrl = useCallback(() => {
    if (process.env.NEXT_PUBLIC_WS_URL) {
      return process.env.NEXT_PUBLIC_WS_URL;
    }

    const apiUrl = process.env.NEXT_PUBLIC_API_URL;
    if (apiUrl && /^https?:\/\//.test(apiUrl)) {
      const u = new URL(apiUrl);
      u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:';
      return `${u.origin}/api/websocket/stream`;
    }

    return 'ws://127.0.0.1:8000/api/websocket/stream';
  }, []);

  const normalizeTicker = useCallback((value: unknown): Record<string, unknown> | null => {
    if (!isObject(value)) return null;
    const symbol = typeof value.symbol === 'string' ? value.symbol : null;
    if (!symbol) return null;

    const ltpRaw = value.ltp ?? value.lp;
    const changePctRaw = value.change_pct ?? value.chp ?? value.changePercent;
    const volumeRaw = value.volume ?? value.v ?? value.vol_traded_today;

    return {
      ...value,
      symbol,
      ltp: typeof ltpRaw === 'number' ? ltpRaw : undefined,
      change_pct: typeof changePctRaw === 'number' ? changePctRaw : undefined,
      volume: typeof volumeRaw === 'number' ? volumeRaw : undefined,
    };
  }, []);

  const emitMessage = useCallback((message: WebSocketMessage) => {
    emitToCallbacks(callbacksRef.current, message);

    if (!optionsRef.current.skipStateUpdates) {
      setLastMessage(message);
    }
  }, []);

  const startHeartbeat = useCallback((socket: WebSocket) => {
    if (heartbeatIntervalRef.current) {
      clearInterval(heartbeatIntervalRef.current);
    }
    heartbeatIntervalRef.current = setInterval(() => {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(PING_EVENT);
      }
    }, HEARTBEAT_INTERVAL_MS);
  }, []);

  const flushQueuedMessages = useCallback(() => {
    if (cancelledRef.current || document.hidden || messagesQueueRef.current.length === 0) {
      return;
    }

    const queuedMessages = messagesQueueRef.current;
    messagesQueueRef.current = [];
    for (const message of queuedMessages) {
      dispatchIncomingMessage(message, normalizeTicker, emitMessage);
    }
  }, [normalizeTicker, emitMessage]);

  // Single effect for connection lifecycle - runs once on mount
  useEffect(() => {
    cancelledRef.current = false;
    const handleVisibilityChange = () => {
      if (!document.hidden) {
        flushQueuedMessages();
      }
    };
    document.addEventListener('visibilitychange', handleVisibilityChange);

    function connectImpl() {
      // Guard: cancelled (unmounted), already open, or already connecting
      if (cancelledRef.current) return;
      if (
        isConnectingRef.current ||
        socketRef.current?.readyState === WebSocket.OPEN ||
        socketRef.current?.readyState === WebSocket.CONNECTING
      ) {
        return;
      }

      // Clear any pending reconnect
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
        reconnectTimeoutRef.current = null;
      }

      isConnectingRef.current = true;
      const wsUrl = getWsUrl();

      try {
        const socket = new WebSocket(wsUrl);
        socketRef.current = socket;

        socket.onopen = () => {
          if (cancelledRef.current) {
            socket.close(1000);
            return;
          }
          isConnectingRef.current = false;
          retryCountRef.current = 0;
          setIsConnected(true);

          startHeartbeat(socket);
        };

        socket.onmessage = (event) => {
          if (cancelledRef.current) return;

          try {
            if (event.data === PONG_EVENT) return;
            if (event.data === PING_EVENT) {
              socket.send(PONG_PAYLOAD);
              return;
            }

            const message = JSON.parse(event.data) as WebSocketMessage;
            if (document.hidden) {
              messagesQueueRef.current.push(message);
              if (messagesQueueRef.current.length > MAX_HIDDEN_QUEUE_SIZE) {
                const overflow = messagesQueueRef.current.length - MAX_HIDDEN_QUEUE_SIZE;
                messagesQueueRef.current.splice(0, overflow);
              }
              return;
            }
            dispatchIncomingMessage(message, normalizeTicker, emitMessage);
          } catch (err) {
            console.warn('[WebSocket] Message parse error:', err);
          }
        };

        socket.onclose = (event) => {
          if (cancelledRef.current) return;

          setIsConnected(false);
          socketRef.current = null;
          isConnectingRef.current = false;
          if (heartbeatIntervalRef.current) clearInterval(heartbeatIntervalRef.current);

          // Only reconnect on abnormal closure (not intentional unmount code 1000)
          if (event.code !== 1000 && retryCountRef.current < MAX_RETRIES) {
            const timeout = Math.min(2000 * Math.pow(2, retryCountRef.current), 30000);
            retryCountRef.current += 1;
            reconnectTimeoutRef.current = setTimeout(connectImpl, timeout);
          }
        };

        socket.onerror = (event) => {
          console.warn('[WebSocket] Connection error:', {
            type: event.type,
            timeStamp: event.timeStamp,
            url: socket.url,
            readyState: socket.readyState,
          });
        };

      } catch (err) {
        console.warn('[WebSocket] Connection creation failed:', err);
        isConnectingRef.current = false;
        if (!cancelledRef.current) {
          reconnectTimeoutRef.current = setTimeout(connectImpl, RECONNECT_INTERVAL);
        }
      }
    }

    connectImpl();

    return () => {
      // Mark as cancelled BEFORE closing - prevents onclose from triggering reconnect
      cancelledRef.current = true;
      isConnectingRef.current = false;
      messagesQueueRef.current = [];

      if (socketRef.current) {
        socketRef.current.close(1000, 'Component unmounted');
        socketRef.current = null;
      }
      if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current);
      if (heartbeatIntervalRef.current) clearInterval(heartbeatIntervalRef.current);
      document.removeEventListener('visibilitychange', handleVisibilityChange);
    };
  }, [getWsUrl, normalizeTicker, emitMessage, startHeartbeat, flushQueuedMessages]);

  const sendMessage = useCallback((message: unknown) => {
    if (socketRef.current?.readyState === WebSocket.OPEN) {
      const payload = typeof message === 'string' ? message : JSON.stringify(message);
      socketRef.current.send(payload);
    } else {
      console.warn('[WebSocket] Cannot send: Not connected');
    }
  }, []);

  const registerCallback = useCallback((cb: (msg: WebSocketMessage) => void) => {
    callbacksRef.current.add(cb);
    return () => callbacksRef.current.delete(cb);
  }, []);

  return { isConnected, lastMessage, sendMessage, registerCallback };
}
