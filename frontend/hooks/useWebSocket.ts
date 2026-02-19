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

interface WebSocketOptions {
  skipStateUpdates?: boolean;
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

  // Single effect for connection lifecycle - runs once on mount
  useEffect(() => {
    // Local cancellation flag - scoped to this effect lifecycle
    let cancelled = false;

    function connectImpl() {
      // Guard: cancelled (unmounted), already open, or already connecting
      if (cancelled) return;
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
          if (cancelled) {
            socket.close(1000);
            return;
          }
          isConnectingRef.current = false;
          retryCountRef.current = 0;
          setIsConnected(true);

          // Start heartbeat
          if (heartbeatIntervalRef.current) clearInterval(heartbeatIntervalRef.current);
          heartbeatIntervalRef.current = setInterval(() => {
            if (socket.readyState === WebSocket.OPEN) {
              socket.send('ping');
            }
          }, 30000);
        };

        socket.onmessage = (event) => {
          if (cancelled) return;

          try {
            if (event.data === '{"type":"pong"}') return;
            if (event.data === 'ping') {
              socket.send(JSON.stringify({ type: 'pong' }));
              return;
            }

            const message = JSON.parse(event.data);

            // Visibility check: pause updates if tab hidden to save CPU
            if (document.hidden) return;

            const handleMsg = (msg: WebSocketMessage) => {
              callbacksRef.current.forEach(cb => {
                try { cb(msg); } catch { /* callback isolation */ }
              });

              // Use ref for live options value
              if (!optionsRef.current.skipStateUpdates) {
                setLastMessage(msg);
              }
            };

            if (message.type === 'ticker_batch' && Array.isArray(message.data)) {
              message.data.forEach((tick: unknown) => {
                const normalized = normalizeTicker(tick);
                if (normalized) {
                  handleMsg({ type: 'ticker', data: normalized });
                } else if (isTickerData(tick)) {
                  handleMsg({ type: 'ticker', data: tick });
                }
              });
            } else if (message.type === 'ticker' && message.data) {
              const normalized = normalizeTicker(message.data);
              handleMsg(normalized ? { ...message, data: normalized } : message);
            } else {
              handleMsg(message);
            }
          } catch (err) {
            console.warn('[WebSocket] Message parse error:', err);
          }
        };

        socket.onclose = (event) => {
          if (cancelled) return;

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
        if (!cancelled) {
          reconnectTimeoutRef.current = setTimeout(connectImpl, RECONNECT_INTERVAL);
        }
      }
    }

    connectImpl();

    return () => {
      // Mark as cancelled BEFORE closing - prevents onclose from triggering reconnect
      cancelled = true;
      isConnectingRef.current = false;

      if (socketRef.current) {
        socketRef.current.close(1000, 'Component unmounted');
        socketRef.current = null;
      }
      if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current);
      if (heartbeatIntervalRef.current) clearInterval(heartbeatIntervalRef.current);
    };
  }, [getWsUrl, normalizeTicker]);

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
