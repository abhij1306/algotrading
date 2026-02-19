/**
 * Backtest API Client
 * ===================
 * Frontend API integration for backtest backend.
 */

import { BacktestConfig, BacktestResult, BacktestListItem } from './types';
import { SavedBacktestConfig, StrategyInfo, UniverseInfo } from '@/lib/types/api';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

// ============================================================================
// Types
// ============================================================================

interface ApiResponse<T> {
  success: boolean;
  data?: T;
  error?: string;
}

interface RunBacktestRequest {
  name?: string;
  asset_type: string;
  strategy: string;
  date_range: { start: string; end: string };
  initial_capital: number;
  costs: {
    brokerage: number;
    slippage: number;
    stamp_duty: number;
  };
  stock_config?: {
    symbols: string[];
    position_sizing: { type: string; value: number };
    max_positions: number;
    long_short: string;
  };
  option_config?: {
    underlying: string;
    option_type: string;
    strike_selection: string;
    expiry_selection: string;
    roll_strategy: string;
  };
  index_config?: {
    universe: string;
    reconstruction: boolean;
    selection_criteria: { type: string; metric: string; n: number };
    rebalancing: { frequency: string; day_of_month: number };
  };
  use_mock_data?: boolean;
}

// ============================================================================
// Helper Functions
// ============================================================================

async function fetchApi<T>(
  endpoint: string,
  options?: RequestInit
): Promise<ApiResponse<T>> {
  try {
    const response = await fetch(`${API_BASE}${endpoint}`, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        ...options?.headers,
      },
    });

    if (!response.ok) {
      const error = await response.text();
      return { success: false, error };
    }

    const data = await response.json();
    return { success: true, data };
  } catch (error) {
    return {
      success: false,
      error: error instanceof Error ? error.message : 'Network error',
    };
  }
}

// ============================================================================
// Backtest API
// ============================================================================

export const backtestApi = {
  /**
   * Run a new backtest (async - returns run_id immediately)
   */
  async runBacktest(config: BacktestConfig): Promise<ApiResponse<{ run_id: string; estimated_time: number }>> {
    const body: RunBacktestRequest = {
      name: config.name || `${config.assetType} Strategy`,
      asset_type: config.assetType,
      strategy: config.assetType === 'option' ? config.strategy : 'momentum',
      date_range: config.dateRange,
      initial_capital: config.initialCapital,
      costs: {
        brokerage: config.costs.brokerage,
        slippage: config.costs.slippage,
        stamp_duty: config.costs.stampDuty || 0.0002,
      },
      use_mock_data: true, // Fallback to mock if real data unavailable
    };

    // Add asset-specific config
    if (config.assetType === 'stock') {
      body.stock_config = {
        symbols: config.symbols || ['NIFTY'],
        position_sizing: config.positionSizing || { type: 'percent_of_equity', value: 100 },
        max_positions: config.maxPositions || 10,
        long_short: config.longShort || 'long',
      };
    } else if (config.assetType === 'option') {
      body.option_config = {
        underlying: config.underlying || 'NIFTY',
        option_type: config.optionSelection?.type || 'CE',
        strike_selection: config.optionSelection?.strikeSelection || 'ATM',
        expiry_selection: config.optionSelection?.expirySelection || 'weekly',
        roll_strategy: config.optionSelection?.rollStrategy || 'at_expiry',
      };
    } else if (config.assetType === 'index') {
      body.index_config = {
        universe: config.universe || 'NIFTY50',
        reconstruction: config.reconstruction !== false,
        selection_criteria: config.selectionCriteria || { type: 'top_n', metric: 'momentum', n: 10 },
        rebalancing: config.rebalancing || { frequency: 'monthly', day_of_month: 1 },
      };
    }

    return fetchApi('/api/backtest/v2/run', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  },

  /**
   * Run backtest synchronously (for development)
   */
  async runBacktestSync(config: BacktestConfig): Promise<ApiResponse<{ run_id: string; metrics: Record<string, unknown> }>> {
    const body: RunBacktestRequest = {
      name: config.name || `${config.assetType} Strategy`,
      asset_type: config.assetType,
      strategy: config.assetType === 'option' ? config.strategy : 'momentum',
      date_range: config.dateRange,
      initial_capital: config.initialCapital,
      costs: {
        brokerage: config.costs.brokerage,
        slippage: config.costs.slippage,
        stamp_duty: config.costs.stampDuty || 0.0002,
      },
    };

    if (config.assetType === 'index') {
      body.index_config = {
        universe: config.universe || 'NIFTY50',
        reconstruction: true,
        selection_criteria: { type: 'top_n', metric: 'momentum', n: 10 },
        rebalancing: { frequency: 'monthly', day_of_month: 1 },
      };
    }

    return fetchApi('/api/backtest/v2/run-sync', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  },

  /**
   * Get backtest results by run_id
   */
  async getResults(runId: string): Promise<ApiResponse<BacktestResult>> {
    return fetchApi(`/api/backtest/v2/results/${runId}`);
  },

  /**
   * List backtest runs
   */
  async listRuns(params?: {
    asset_type?: string;
    strategy?: string;
    status?: string;
    limit?: number;
    offset?: number;
  }): Promise<ApiResponse<BacktestListItem[]>> {
    const queryParams = new URLSearchParams();
    if (params?.asset_type) queryParams.set('asset_type', params.asset_type);
    if (params?.strategy) queryParams.set('strategy', params.strategy);
    if (params?.status) queryParams.set('status', params.status);
    if (params?.limit) queryParams.set('limit', params.limit.toString());
    if (params?.offset) queryParams.set('offset', params.offset.toString());

    const query = queryParams.toString();
    return fetchApi(`/api/backtest/v2/runs${query ? `?${query}` : ''}`);
  },

  /**
   * Delete a backtest run
   */
  async deleteRun(runId: string): Promise<ApiResponse<{ message: string }>> {
    return fetchApi(`/api/backtest/v2/runs/${runId}`, {
      method: 'DELETE',
    });
  },

  /**
   * Save a configuration for later use
   */
  async saveConfig(params: {
    name: string;
    config: BacktestConfig;
    asset_type: string;
    strategy: string;
    description?: string;
    tags?: string[];
  }): Promise<ApiResponse<{ id: number }>> {
    return fetchApi('/api/backtest/v2/configs', {
      method: 'POST',
      body: JSON.stringify({
        name: params.name,
        config: params.config,
        asset_type: params.asset_type,
        strategy: params.strategy,
        description: params.description,
        tags: params.tags,
      }),
    });
  },

  /**
   * List saved configurations
   */
  async listSavedConfigs(asset_type?: string): Promise<ApiResponse<SavedBacktestConfig[]>> {
    const query = asset_type ? `?asset_type=${asset_type}` : '';
    return fetchApi(`/api/backtest/v2/configs${query}`);
  },

  /**
   * Create a comparison of multiple runs
   */
  async createComparison(params: {
    name: string;
    runIds: string[];
    description?: string;
  }): Promise<ApiResponse<{ id: number; comparison: Record<string, unknown> }>> {
    return fetchApi('/api/backtest/v2/comparisons', {
      method: 'POST',
      body: JSON.stringify({
        name: params.name,
        run_ids: params.runIds,
        description: params.description,
      }),
    });
  },

  /**
   * Get comparison details
   */
  async getComparison(comparisonId: number): Promise<ApiResponse<Record<string, unknown>>> {
    return fetchApi(`/api/backtest/v2/comparisons/${comparisonId}`);
  },
};

// ============================================================================
// Legacy API (V1)
// ============================================================================

export const backtestApiV1 = {
  /**
   * Run portfolio backtest using V1 API
   */
  async runPortfolioBacktest(params: {
    universe: string;
    start_date: string;
    end_date: string;
    initial_capital: number;
    strategy: string;
    rebalance_frequency: string;
    max_positions: number;
    brokerage: number;
    slippage: number;
  }): Promise<ApiResponse<Record<string, unknown>>> {
    return fetchApi('/api/backtest/run', {
      method: 'POST',
      body: JSON.stringify(params),
    });
  },

  /**
   * Get backtest results (V1)
   */
  async getResults(runId: string): Promise<ApiResponse<Record<string, unknown>>> {
    return fetchApi(`/api/backtest/results/${runId}`);
  },

  /**
   * List backtest runs (V1)
   */
  async listRuns(limit: number = 20): Promise<ApiResponse<{ runs: Array<Record<string, unknown>> }>> {
    return fetchApi(`/api/backtest/runs?limit=${limit}`);
  },

  /**
   * Get available strategies
   */
  async getStrategies(): Promise<ApiResponse<{ strategies: StrategyInfo[] }>> {
    return fetchApi('/api/backtest/strategies');
  },

  /**
   * Get available universes
   */
  async getUniverses(): Promise<ApiResponse<{ universes: UniverseInfo[] }>> {
    return fetchApi('/api/backtest/universes');
  },
};

export default backtestApi;
