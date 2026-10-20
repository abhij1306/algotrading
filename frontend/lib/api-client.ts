/**
 * Centralized API Client for SmartTrader 3.0
 * Provides unified error handling, loading states, and retry logic
 */

// Default to same-origin so the Vite dev proxy forwards `/api/*` to the backend
// without CORS issues. Set VITE_API_URL to call a backend on another origin.
const API_BASE_URL = import.meta.env.VITE_API_URL || "";

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
  private readonly baseURL: string;

  constructor(baseURL: string = API_BASE_URL) {
    this.baseURL = baseURL;
  }

  /**
   * Generic request method with error handling
   */
  private async request<T>(endpoint: string, options: RequestInit = {}): Promise<APIResponse<T>> {
    try {
      const url = joinUrl(this.baseURL, endpoint);
      const headers = new Headers(options.headers);
      if (!headers.has("Content-Type")) headers.set("Content-Type", "application/json");
      const response = await fetch(url, { ...options, headers });

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
    maxRetries: number = 3,
  ): Promise<APIResponse<T>> {
    return this.attempt<T>({ endpoint, options, attempt: 0, maxRetries });
  }

  /** One try, then (sequentially, by design) the next after an exponential backoff. */
  private async attempt<T>(input: {
    endpoint: string;
    options: RequestInit;
    attempt: number;
    maxRetries: number;
    lastError?: APIError;
  }): Promise<APIResponse<T>> {
    const { endpoint, options, attempt, maxRetries, lastError } = input;
    if (attempt >= maxRetries) return { error: lastError };

    const result = await this.request<T>(endpoint, options);
    if (result.data) {
      return result; // Success
    }

    // Don't retry on client errors (4xx)
    if (result.error?.code === "VALIDATION_ERROR" || result.error?.code === "DATA_NOT_FOUND") {
      return { error: result.error };
    }

    // Wait before retrying (exponential backoff)
    if (attempt < maxRetries - 1) {
      await new Promise((resolve) => setTimeout(resolve, Math.pow(2, attempt) * 1000));
    }
    return this.attempt<T>({ ...input, attempt: attempt + 1, lastError: result.error });
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
    apiClient.get(
      `/api/strategies/vcp/scan/latest?universe=${encodeURIComponent(universe)}&show_all=${showAll}`,
    ),
  runScan: (body?: Record<string, unknown>) =>
    apiClient.post("/api/strategies/vcp/scan/run", body ?? {}),
  getSignal: (symbol: string) =>
    apiClient.get(`/api/strategies/vcp/signal/${encodeURIComponent(symbol)}`),
  queueSignal: (signalId: number) => apiClient.post(`/api/strategies/vcp/signal/${signalId}/queue`),
  cancelSignal: (signalId: number) =>
    apiClient.post(`/api/strategies/vcp/signal/${signalId}/cancel`),
  getPositions: () => apiClient.get("/api/strategies/positions"),
  closePosition: (positionId: number) =>
    apiClient.post(`/api/strategies/positions/${positionId}/close`),
  updateStop: (positionId: number, stop_price: number) =>
    apiClient.post(`/api/strategies/positions/${positionId}/stop`, { stop_price }),
  halt: (reason?: string) => apiClient.post("/api/strategies/halt", reason ? { reason } : {}),
  resume: () => apiClient.post("/api/strategies/resume"),
  runBacktest: (body: Record<string, unknown>) =>
    apiClient.post("/api/strategies/vcp/backtest/run", body),
  getBacktestHistory: (universe?: string) => {
    const query = universe ? `?universe=${encodeURIComponent(universe)}` : "";
    return apiClient.get(`/api/strategies/vcp/backtest/history${query}`);
  },
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
