// WebSocket functionality disabled - using simple polling for reliability
// This file kept for potential future reactivation
export function useWebSocket() {
    return {
        isConnected: false,
        lastMessage: null,
        sendMessage: () => { }
    }
}
