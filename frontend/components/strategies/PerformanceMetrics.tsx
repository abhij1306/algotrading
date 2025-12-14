import { TrendingUp, CheckCircle, Scale, Droplet } from 'lucide-react';
import { format, parseISO } from 'date-fns';

interface PerformanceMetricsProps {
    results: any;
}

export default function PerformanceMetrics({ results }: PerformanceMetricsProps) {
    if (!results) {
        return null;
    }

    const metrics = results.metrics?.performance || {};
    const risk = results.metrics?.risk || {};
    const summary = results.summary || {};

    // Calculate Net Profit
    const netProfit = (results.final_capital || 0) - (results.initial_capital || 0);
    const isProfit = netProfit >= 0;
    const totalReturn = results.total_return || 0;

    // Format dates for Max Drawdown
    let drawdownPeriod = "High Risk Period";
    if (results.metrics?.risk?.max_drawdown_start_date && results.metrics?.risk?.max_drawdown_end_date) {
        try {
            const start = parseISO(results.metrics.risk.max_drawdown_start_date);
            const end = parseISO(results.metrics.risk.max_drawdown_end_date);
            drawdownPeriod = `Occurred ${format(start, 'MMM dd')} - ${format(end, 'MMM dd')}`;
        } catch (e) {
            console.error("Error formatting dates", e);
        }
    }

    return (
        <div className="grid grid-cols-4 gap-4 mb-6">
            {/* Box 1: Net Profit */}
            <div className="bg-card-dark rounded-xl border border-border-dark p-5 shadow-sm relative overflow-hidden group">
                <div className="flex justify-between items-start mb-2">
                    <div className="text-xs text-text-secondary font-medium uppercase tracking-wider opacity-70">Net Profit</div>
                    <div className={`p-1.5 rounded-lg ${isProfit ? 'bg-green-500/10' : 'bg-red-500/10'}`}>
                        <TrendingUp className={`w-4 h-4 ${isProfit ? 'text-green-500' : 'text-red-500'}`} />
                    </div>
                </div>
                <div className="space-y-1">
                    <div className="flex items-baseline gap-2">
                        <span className={`text-2xl font-bold tracking-tight ${isProfit ? 'text-white' : 'text-red-500'}`}>
                            {isProfit ? '+' : '-'}₹{Math.abs(netProfit).toLocaleString('en-IN', { maximumFractionDigits: 0 })}
                        </span>
                        <span className={`text-sm font-bold ${isProfit ? 'text-green-500' : 'text-red-500'}`}>
                            {isProfit ? '+' : ''}{totalReturn.toFixed(1)}%
                        </span>
                    </div>


                    <div className="text-[10px] text-text-secondary opacity-60">
                        {metrics.cagr_pct !== undefined && metrics.cagr_pct !== 0
                            ? `CAGR: ${metrics.cagr_pct > 0 ? '+' : ''}${metrics.cagr_pct.toFixed(1)}%`
                            : `Sharpe: ${(metrics.sharpe_ratio || 0).toFixed(2)}`}
                    </div>
                </div>
            </div>

            {/* Box 2: Win Rate */}
            <div className="bg-card-dark rounded-xl border border-border-dark p-5 shadow-sm relative overflow-hidden group">
                <div className="flex justify-between items-start mb-2">
                    <div className="text-xs text-text-secondary font-medium uppercase tracking-wider opacity-70">Win Rate</div>
                    <div className="p-1.5 rounded-lg bg-blue-500/10">
                        <CheckCircle className="w-4 h-4 text-blue-500" />
                    </div>
                </div>
                <div className="space-y-1">
                    <div className="text-2xl font-bold text-white tracking-tight">
                        {(metrics.win_rate_pct || 0).toFixed(1)}%
                    </div>
                    <div className="text-xs text-text-secondary opacity-60">
                        {summary.winning_trades || 0} Winning Trades
                    </div>
                </div>
            </div>

            {/* Box 3: Profit Factor */}
            <div className="bg-card-dark rounded-xl border border-border-dark p-5 shadow-sm relative overflow-hidden group">
                <div className="flex justify-between items-start mb-2">
                    <div className="text-xs text-text-secondary font-medium uppercase tracking-wider opacity-70">Profit Factor</div>
                    <div className="p-1.5 rounded-lg bg-orange-500/10">
                        <Scale className="w-4 h-4 text-orange-500" />
                    </div>
                </div>
                <div className="space-y-1">
                    <div className="text-2xl font-bold text-white tracking-tight">
                        {(metrics.profit_factor || 0).toFixed(2)}
                    </div>
                    <div className="text-[10px] text-text-secondary opacity-60 truncate">
                        Avg Win ₹{Math.round(summary.avg_win || 0)} / Avg Loss ₹{Math.round(Math.abs(summary.avg_loss || 0))}
                    </div>
                </div>
            </div>

            {/* Box 4: Max Drawdown */}
            <div className="bg-card-dark rounded-xl border border-border-dark p-5 shadow-sm relative overflow-hidden group">
                <div className="flex justify-between items-start mb-2">
                    <div className="text-xs text-text-secondary font-medium uppercase tracking-wider opacity-70">Max Drawdown</div>
                    <div className="p-1.5 rounded-lg bg-red-500/10">
                        <Droplet className="w-4 h-4 text-red-500" />
                    </div>
                </div>
                <div className="space-y-1">
                    <div className="text-2xl font-bold text-white tracking-tight">
                        {(risk.max_drawdown_pct || 0).toFixed(1)}%
                    </div>
                    <div className="text-xs text-text-secondary opacity-60">
                        {drawdownPeriod}
                    </div>
                </div>
            </div>
        </div>
    );
}
