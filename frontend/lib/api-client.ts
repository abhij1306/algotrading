/**
 * Centralized API Client for SmartTrader 3.0
 * Provides unified error handling, loading states, and retry logic
 */

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:9000';

export interface APIError {
    code: string;
    message: string;
    details?: Record<string, any>;
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
    private async request<T>(
        endpoint: string,
        options: RequestInit = {}
    ): Promise<APIResponse<T>> {
        try {
            const url = `${this.baseURL}${endpoint}`;
            const response = await fetch(url, {
                ...options,
                headers: {
                    'Content-Type': 'application/json',
                    ...options.headers,
                },
            });

            // Handle non-OK responses
            if (!response.ok) {
                const errorData = await response.json().catch(() => ({
                    error: {
                        code: 'UNKNOWN_ERROR',
                        message: `HTTP ${response.status}: ${response.statusText}`,
                    },
                }));

                return {
                    error: errorData.error || {
                        code: 'HTTP_ERROR',
                        message: `Request failed with status ${response.status}`,
                    },
                };
            }

            // Parse JSON response
            const data = await response.json();
            return { data };
        } catch (error) {
            console.error('API Request Error:', error);
            return {
                error: {
                    code: 'NETWORK_ERROR',
                    message: error instanceof Error ? error.message : 'Network request failed',
                },
            };
        }
    }

    /**
     * GET request
     */
    async get<T>(endpoint: string): Promise<APIResponse<T>> {
        return this.request<T>(endpoint, { method: 'GET' });
    }

    /**
     * POST request
     */
    async post<T>(endpoint: string, body?: any): Promise<APIResponse<T>> {
        return this.request<T>(endpoint, {
            method: 'POST',
            body: body ? JSON.stringify(body) : undefined,
        });
    }

    /**
     * PATCH request
     */
    async patch<T>(endpoint: string, body?: any): Promise<APIResponse<T>> {
        return this.request<T>(endpoint, {
            method: 'PATCH',
            body: body ? JSON.stringify(body) : undefined,
        });
    }

    /**
     * DELETE request
     */
    async delete<T>(endpoint: string): Promise<APIResponse<T>> {
        return this.request<T>(endpoint, { method: 'DELETE' });
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
            if (lastError?.code === 'VALIDATION_ERROR' || lastError?.code === 'DATA_NOT_FOUND') {
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

// Convenience functions for specific endpoints
export const portfolioAPI = {
    // Stock Portfolios (Analyst Mode)
    getStockPortfolios: () => apiClient.get('/api/portfolio/stocks'),
    getStockPortfolio: (id: number) => apiClient.get(`/api/portfolio/stocks/${id}`),
    createStockPortfolio: (data: any) => apiClient.post('/api/portfolio/stocks', data),
    deleteStockPortfolio: (id: number) => apiClient.delete(`/api/portfolio/stocks/${id}`),
    analyzePortfolioRisk: (id: number) => apiClient.post(`/api/portfolio/stocks/${id}/analyze`),

    // Strategy Portfolios (Quant Mode)
    getStrategyPortfolios: () => apiClient.get('/api/portfolio/strategies'),
    createStrategyPortfolio: (data: any) => apiClient.post('/api/portfolio/strategies', data),
    getAvailableStrategies: () => apiClient.get('/api/portfolio/strategies/available'),
    updateStrategyMetadata: (id: string, data: any) =>
        apiClient.patch(`/api/portfolio/strategies/${id}`, data),
    calculateCorrelation: (strategyIds: string[]) =>
        apiClient.post('/api/portfolio/strategies/correlation', { strategy_ids: strategyIds }),
    runBacktest: (data: any) => apiClient.post('/api/portfolio/strategies/backtest', data),

    // Portfolio Policies
    getPolicies: () => apiClient.get('/api/portfolio/strategies/policy'),
    createPolicy: (data: any) => apiClient.post('/api/portfolio/strategies/policy', data),

    // Live Monitoring
    getMonitoringData: () => apiClient.get('/api/portfolio/strategies/monitor'),
    refreshMonitoring: () => apiClient.post('/api/portfolio/strategies/monitor/refresh'),
};

export const screenerAPI = {
    getStocks: (filters?: any) => {
        const query = filters ? `?${new URLSearchParams(filters).toString()}` : '';
        return apiClient.get(`/api/screener/${query}`);
    },
    getIndices: () => apiClient.get('/api/screener/indices'),
};

export const marketAPI = {
    getLiveQuotes: (symbols: string[]) =>
        apiClient.get(`/api/market/quotes/live?symbols=${symbols.join(',')}`),
    searchSymbol: (query: string) => apiClient.get(`/api/market/search?q=${query}`),
    getSectors: () => apiClient.get('/api/market/sectors'),
    getWatchlist: () => apiClient.get('/api/market/watchlist'),
    addToWatchlist: (symbol: string) => apiClient.post('/api/market/watchlist', { symbol }),
    removeFromWatchlist: (symbol: string) => apiClient.delete(`/api/market/watchlist/${symbol}`),
};

// Error handling utilities
export function getErrorMessage(error?: APIError): string {
    if (!error) return 'An unknown error occurred';
    return error.message || 'Request failed';
}

export function isNetworkError(error?: APIError): boolean {
    return error?.code === 'NETWORK_ERROR';
}

export function isNotFoundError(error?: APIError): boolean {
    return error?.code === 'DATA_NOT_FOUND';
}

export function isValidationError(error?: APIError): boolean {
    return error?.code === 'VALIDATION_ERROR';
}
