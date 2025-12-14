interface PerformanceMetricsProps {
    metrics: any;
}

export default function PerformanceMetrics({ metrics }: PerformanceMetricsProps) {
    if (!metrics) return null;

    return (
        <div className="bg-slate-800/50 rounded-lg p-6 border border-slate-700">
            <h3 className="text-sm font-semibold text-slate-400 mb-4 flex items-center">
                <svg className="w-4 h-4 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6" />
                </svg>
                PERFORMANCE
            </h3>

            <div className="space-y-4">
                <div className="grid grid-cols-2 gap-4">
                    <div>
                        <div className="text-sm text-slate-400">Win Rate</div>
                        <div className={`text-2xl font-bold mt-1 ${(metrics.win_rate_pct || 0) >= 50 ? 'text-green-400' : 'text-red-400'
                            }`}>
                            {(metrics.win_rate_pct || 0).toFixed(0)}%
                        </div>
                    </div>

                    <div>
                        <div className="text-sm text-slate-400">Profit Factor</div>
                        <div className={`text-2xl font-bold mt-1 ${(metrics.profit_factor || 0) >= 1 ? 'text-green-400' : 'text-red-400'
                            }`}>
                            {(metrics.profit_factor || 0).toFixed(2)}
                        </div>
                    </div>
                </div>

                <div className="pt-4 border-t border-slate-700 space-y-3">
                    <div className="flex justify-between text-sm">
                        <span className="text-slate-400">CAGR</span>
                        <span className={`font-medium ${(metrics.cagr_pct || 0) >= 0 ? 'text-green-400' : 'text-red-400'
                            }`}>
                            {(metrics.cagr_pct || 0).toFixed(1)}%
                        </span>
                    </div>

                    <div className="flex justify-between text-sm">
                        <span className="text-slate-400">Sharpe Ratio</span>
                        <span className="text-white font-medium">
                            {(metrics.sharpe_ratio || 0).toFixed(2)}
                        </span>
                    </div>
                </div>
            </div>
        </div>
    );
}
