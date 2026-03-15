/**
 * Centralized API Client for SmartTrader 3.0
 * Provides unified error handling, loading states, and retry logic
 */

// Default to same-origin in the browser so Next.js rewrites can proxy `/api/*`
// to the backend without CORS issues. On the server (SSR/build), fall back to a
// loopback backend URL unless explicitly configured.
const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || (globalThis.window === undefined ? "http://127.0.0.1:8000" : "");

function joinUrl(baseURL: string, endpoint: string): string {
  // Allow relative baseURL (''), and avoid accidental double slashes.
  if (!baseURL) return endpoint;
  const base = baseURL.endsWith("/") ? baseURL.slice(0, -1) : baseURL;
  const path = endpoint.startsWith("/") ? endpoint : `/${endpoint}`;
  return `${base}${path}`;
}

export interface APIError {
  code: string;
  message: string;
  details?: Record<string, unknown>;
}

export interface APIResponse<T> {
  data?: T;
  error?: APIError;
}

class SmartTraderAPIClient {
  private baseURL: string;

  constructor(baseURL: string = API_BASE_URL) {
    this.baseURL = baseURL;
  }

  /**
   * Generic request method with error handling
   */
  private async request<T>(endpoint: string, options: RequestInit = {}): Promise<APIResponse<T>> {
    try {
      const url = joinUrl(this.baseURL, endpoint);
      const response = await fetch(url, {
        ...options,
        headers: {
          "Content-Type": "application/json",
          ...options.headers,
        },
      });

      // Handle non-OK responses
      if (!response.ok) {
        const errorData = await response.json().catch(() => ({
          error: {
            code: "UNKNOWN_ERROR",
            message: `HTTP ${response.status}: ${response.statusText}`,
          },
        }));

        return {
          error: errorData.error || {
            code: "HTTP_ERROR",
            message: `Request failed with status ${response.status}`,
          },
        };
      }

      // Parse JSON response
      const data = await response.json();
      return { data };
    } catch (error) {
      const url = joinUrl(this.baseURL, endpoint);
      console.error("API Request Error:", { url, error });
      return {
        error: {
          code: "NETWORK_ERROR",
          message:
            error instanceof Error
              ? `${error.message} (${url})`
              : `Network request failed (${url})`,
        },
      };
    }
  }

  /**
   * GET request
   */
  async get<T>(endpoint: string): Promise<APIResponse<T>> {
    return this.request<T>(endpoint, { method: "GET" });
  }

  /**
   * POST request
   */
  async post<T>(endpoint: string, body?: unknown): Promise<APIResponse<T>> {
    return this.request<T>(endpoint, {
      method: "POST",
      body: body ? JSON.stringify(body) : undefined,
    });
  }

  /**
   * PATCH request
   */
  async patch<T>(endpoint: string, body?: unknown): Promise<APIResponse<T>> {
    return this.request<T>(endpoint, {
      method: "PATCH",
      body: body ? JSON.stringify(body) : undefined,
    });
  }

  /**
   * DELETE request
   */
  async delete<T>(endpoint: string): Promise<APIResponse<T>> {
    return this.request<T>(endpoint, { method: "DELETE" });
  }

  /**
   * Request with retry logic
   */
  async requestWithRetry<T>(
    endpoint: string,
    options: RequestInit = {},
    maxRetries: number = 3
  ): Promise<APIResponse<T>> {
    let lastError: APIError | undefined;

    for (let attempt = 0; attempt < maxRetries; attempt++) {
      const result = await this.request<T>(endpoint, options);

      if (result.data) {
        return result; // Success
      }

      lastError = result.error;

      // Don't retry on client errors (4xx)
      if (lastError?.code === "VALIDATION_ERROR" || lastError?.code === "DATA_NOT_FOUND") {
        break;
      }

      // Wait before retrying (exponential backoff)
      if (attempt < maxRetries - 1) {
        await new Promise((resolve) => setTimeout(resolve, Math.pow(2, attempt) * 1000));
      }
    }

    return { error: lastError };
  }
}

// Export singleton instance
export const apiClient = new SmartTraderAPIClient();

export const screenerAPI = {
  getStocks: (filters?: Record<string, string>) => {
    const query = filters ? `?${new URLSearchParams(filters).toString()}` : "";
    return apiClient.get(`/api/screener/results${query}`);
  },
  getIndices: () => apiClient.get("/api/screener/indices"),
};

export const marketAPI = {
  getLiveQuotes: (symbols: string[]) =>
    apiClient.get(`/api/market/quotes/live?symbols=${symbols.join(",")}`),
  searchSymbol: (query: string) => apiClient.get(`/api/market/search?q=${query}`),
  getSectors: () => apiClient.get("/api/market/sectors"),
  getWatchlist: () => apiClient.get("/api/market/watchlist"),
  addToWatchlist: (symbol: string) => apiClient.post("/api/market/watchlist", { symbol }),
  removeFromWatchlist: (symbol: string) => apiClient.delete(`/api/market/watchlist/${symbol}`),
};

export const strategiesAPI = {
  getStatus: (universe = "NIFTY500") =>
    apiClient.get(`/api/strategies/status?universe=${encodeURIComponent(universe)}`),
  getRegime: () => apiClient.get("/api/strategies/regime"),
  getLatestScan: (universe = "NIFTY500", showAll = false) =>
    apiClient.get(`/api/strategies/vcp/scan/latest?universe=${encodeURIComponent(universe)}&show_all=${showAll}`),
  runScan: (body?: Record<string, unknown>) => apiClient.post("/api/strategies/vcp/scan/run", body ?? {}),
  getSignal: (symbol: string) => apiClient.get(`/api/strategies/vcp/signal/${encodeURIComponent(symbol)}`),
  queueSignal: (signalId: number) => apiClient.post(`/api/strategies/vcp/signal/${signalId}/queue`),
  cancelSignal: (signalId: number) => apiClient.post(`/api/strategies/vcp/signal/${signalId}/cancel`),
  getPositions: () => apiClient.get("/api/strategies/positions"),
  closePosition: (positionId: number) => apiClient.post(`/api/strategies/positions/${positionId}/close`),
  updateStop: (positionId: number, stop_price: number) =>
    apiClient.post(`/api/strategies/positions/${positionId}/stop`, { stop_price }),
  halt: (reason?: string) => apiClient.post("/api/strategies/halt", reason ? { reason } : {}),
  resume: () => apiClient.post("/api/strategies/resume"),
  runBacktest: (body: Record<string, unknown>) => apiClient.post("/api/strategies/vcp/backtest/run", body),
  getBacktestHistory: (universe?: string) =>
    apiClient.get(
      `/api/strategies/vcp/backtest/history${universe ? `?universe=${encodeURIComponent(universe)}` : ""}`
    ),
};

// Error handling utilities
export function getErrorMessage(error?: APIError): string {
  if (!error) return "An unknown error occurred";
  return error.message || "Request failed";
}

export function isNetworkError(error?: APIError): boolean {
  return error?.code === "NETWORK_ERROR";
}

export function isNotFoundError(error?: APIError): boolean {
  return error?.code === "DATA_NOT_FOUND";
}

export function isValidationError(error?: APIError): boolean {
  return error?.code === "VALIDATION_ERROR";
}
