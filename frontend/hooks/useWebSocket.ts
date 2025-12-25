// WebSocket functionality disabled - using simple polling for reliability
// This file kept for potential future reactivation
export function useWebSocket() {
    return {
        isConnected: false,
        // cast to any to prevent TS errors in consumers expecting a message object
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        lastMessage: null as any,
        sendMessage: () => { }
    }
}
