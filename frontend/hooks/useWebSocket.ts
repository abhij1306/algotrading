"use client";

import { useState, useEffect, useCallback, useRef } from 'react';

interface WebSocketMessage {
  type?: string;
  data?: any;
  symbol?: string;
  ltp?: number;
  [key: string]: any;
}

interface WebSocketHookReturn {
  isConnected: boolean;
  lastMessage: WebSocketMessage | null;
  sendMessage: (message: any) => void;
}

/**
 * Creates and manages a persistent WebSocket connection to the backend stream and exposes connection state, the latest received message, and a send helper.
 *
 * Manages automatic reconnects and a heartbeat ping; incoming JSON messages are parsed and stored as the latest message while raw `{"type":"pong"}` pings are ignored.
 *
 * @returns An object containing the WebSocket hook state and helpers:
 * - `isConnected`: `true` when the socket is open, `false` otherwise.
 * - `lastMessage`: the most recently received and parsed WebSocket message, or `null` if none.
 * - `sendMessage`: a function that sends a string or serializable value over the socket.
 */
export function useWebSocket(): WebSocketHookReturn {
  const [isConnected, setIsConnected] = useState(false);
  const [lastMessage, setLastMessage] = useState<WebSocketMessage | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const heartbeatIntervalRef = useRef<NodeJS.Timeout | null>(null);

  const connect = useCallback(() => {
    // Clear any existing timeouts
    if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current);

    try {
      const baseUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
      const wsUrl = baseUrl.replace('http', 'ws') + '/api/websocket/stream';

      console.log(`[WebSocket] Connecting to ${wsUrl}...`);
      const socket = new WebSocket(wsUrl);
      socketRef.current = socket;

      socket.onopen = () => {
        console.log('[WebSocket] Connected');
        setIsConnected(true);

        // Start heartbeat
        heartbeatIntervalRef.current = setInterval(() => {
          if (socket.readyState === WebSocket.OPEN) {
            socket.send('ping');
          }
        }, 30000);
      };

      socket.onmessage = (event) => {
        try {
          if (event.data === '{"type":"pong"}') return;

          const message = JSON.parse(event.data);
          setLastMessage(message);
        } catch (err) {
          console.error('[WebSocket] Error parsing message:', err);
        }
      };

      socket.onclose = (event) => {
        console.log(`[WebSocket] Disconnected: ${event.reason} (${event.code})`);
        setIsConnected(false);
        socketRef.current = null;

        if (heartbeatIntervalRef.current) clearInterval(heartbeatIntervalRef.current);

        // Auto-reconnect after 3 seconds
        reconnectTimeoutRef.current = setTimeout(connect, 3000);
      };

      socket.onerror = (error) => {
        console.error('[WebSocket] Error:', error);
        socket.close();
      };

    } catch (err) {
      console.error('[WebSocket] Connection failed:', err);
      reconnectTimeoutRef.current = setTimeout(connect, 5000);
    }
  }, []);

  useEffect(() => {
    connect();

    return () => {
      if (socketRef.current) {
        socketRef.current.close();
      }
      if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current);
      if (heartbeatIntervalRef.current) clearInterval(heartbeatIntervalRef.current);
    };
  }, [connect]);

  const sendMessage = useCallback((message: any) => {
    if (socketRef.current && socketRef.current.readyState === WebSocket.OPEN) {
      socketRef.current.send(typeof message === 'string' ? message : JSON.stringify(message));
    } else {
      console.warn('[WebSocket] Cannot send message: Not connected');
    }
  }, []);

  return {
    isConnected,
    lastMessage,
    sendMessage
  };
}