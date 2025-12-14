interface RiskProfileProps {
    metrics: any;
}

export default function RiskProfile({ metrics }: RiskProfileProps) {
    if (!metrics) return null;

    return (
        <div className="bg-slate-800/50 rounded-lg p-6 border border-slate-700">
            <h3 className="text-sm font-semibold text-slate-400 mb-4 flex items-center">
                <svg className="w-4 h-4 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                </svg>
                RISK PROFILE
            </h3>

            <div className="space-y-4">
                <div className="grid grid-cols-2 gap-4">
                    <div>
                        <div className="text-sm text-slate-400">Max Drawdown</div>
                        <div className="text-2xl font-bold text-red-400 mt-1">
                            {(metrics.max_drawdown_pct || 0).toFixed(1)}%
                        </div>
                    </div>

                    <div>
                        <div className="text-sm text-slate-400">Max Subs. Losses</div>
                        <div className="text-2xl font-bold text-white mt-1">
                            {metrics.max_consecutive_losses || 0}
                        </div>
                    </div>
                </div>

                <div className="pt-4 border-t border-slate-700 space-y-3">
                    <div className="flex justify-between text-sm">
                        <span className="text-slate-400">Volatility</span>
                        <span className="text-white font-medium">
                            {(metrics.volatility_pct || 0).toFixed(1)}%
                        </span>
                    </div>

                    <div className="flex justify-between text-sm">
                        <span className="text-slate-400">VaR 95%</span>
                        <span className="text-white font-medium">
                            {(metrics.var_95_pct || 0).toFixed(2)}%
                        </span>
                    </div>
                </div>
            </div>
        </div>
    );
}
