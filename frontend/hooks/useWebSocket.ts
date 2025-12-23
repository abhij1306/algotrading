
import { useState, useEffect, useRef, useCallback } from 'react';

const WS_URL = process.env.NEXT_PUBLIC_WS_URL || 'ws://localhost:8000/api/websocket/stream';

export interface TickData {
    symbol: string;
    ltp: number;
    open_price?: number;
    high_price?: number;
    low_price?: number;
    prev_close_price?: number;
    vol_traded_today?: number;
    ch?: number; // Change
    chp?: number; // Change %
}

export function useWebSocket() {
    const [isConnected, setIsConnected] = useState(false);
    const [lastMessage, setLastMessage] = useState<TickData | null>(null);
    const ws = useRef<WebSocket | null>(null);

    useEffect(() => {
        // Initialize WebSocket
        const socket = new WebSocket(WS_URL);

        socket.onopen = () => {
            console.log('WebSocket Connected');
            setIsConnected(true);
        };

        socket.onclose = () => {
            console.log('WebSocket Disconnected');
            setIsConnected(false);
            // Optional: Logic to reconnect could go here
        };

        socket.onerror = (error) => {
            console.error('WebSocket Error:', error);
        };

        socket.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                if (data.type === 'pong') return;

                // Normalize data if needed (Fyers structure)
                // Adjust based on exact Fyers JSON payload
                setLastMessage(data);
            } catch (e) {
                console.error("WS Parse Error", e);
            }
        };

        ws.current = socket;

        return () => {
            socket.close();
        };
    }, []);

    const sendMessage = useCallback((msg: any) => {
        if (ws.current && ws.current.readyState === WebSocket.OPEN) {
            ws.current.send(JSON.stringify(msg));
        }
    }, []);

    return { isConnected, lastMessage, sendMessage };
}
