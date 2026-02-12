// WebSocket functionality disabled - using simple polling for reliability
// This file kept for potential future reactivation

interface WebSocketMessage {
  symbol?: string
  ltp?: number
  [key: string]: unknown
}

interface WebSocketHookReturn {
  isConnected: boolean
  lastMessage: WebSocketMessage | null
  sendMessage: (message: unknown) => void
}

export function useWebSocket(): WebSocketHookReturn {
  return {
    isConnected: false,
    lastMessage: null,
    sendMessage: () => { }
  }
}
