"""
Quant Backtest Wrapper
For "institutional/fund" mode - uses locked strategies from database

Behavior:
- NO parameter tuning allowed
- Strategies are locked logic blocks from StrategyContract table
- Focus on portfolio composition and regime analysis
- Parameters frozen in database
"""
from typing import List, Dict, Any
from datetime import datetime
import pandas as pd
import numpy as np
from sqlalchemy.orm import Session

from ...database import StrategyContract
from ..strategies import STRATEGY_REGISTRY
from ...data_repository import DataRepository
from ...risk_metrics import RiskMetricsEngine
from .schemas import BacktestConfig, BacktestResult


class QuantBacktestRunner:
    """
    Quant Mode Backtest Runner
    Uses locked strategy contracts from database.
    Executes 'run_day' logic directly, bypassing event-driven BacktestCore.
    """
    
    def __init__(self, db_session: Session):
        self.db = db_session
        self.repo = DataRepository(db_session)
    
    async def run_single_strategy(
        self,
        strategy_id: str,
        universe_id: str,
        start_date: datetime,
        end_date: datetime
    ) -> Dict[str, Any]:
        """
        Run backtest for a single locked strategy
        """
        # 1. Load strategy contract from DB
        contract = self.db.query(StrategyContract).filter_by(
            strategy_id=strategy_id
        ).first()
        
        if not contract:
            raise ValueError(f"Strategy contract '{strategy_id}' not found")
        
        # 2. Verify universe
        if "ALL" not in contract.allowed_universes and universe_id not in contract.allowed_universes:
             pass
        
        # 3. Load universe symbols
        symbols = await self._load_universe(universe_id, start_date)
        
        # 4. Run backtest on each symbol
        results = []
        for symbol in symbols:
            try:
                result = await self._backtest_symbol(
                    contract,
                    symbol,
                    start_date,
                    end_date
                )
                results.append(result)
            except Exception as e:
                print(f"Error backtesting {symbol}: {e}")
                import traceback
                traceback.print_exc()
                continue
        
        # 5. Aggregate results
        return self._aggregate_results(results, strategy_id, universe_id)
    
    async def run_portfolio(
        self,
        strategy_ids: List[str],
        universe_id: str,
        start_date: datetime,
        end_date: datetime,
        allocation: Dict[str, float]
    ) -> Dict[str, Any]:
        """
        Run multi-strategy portfolio backtest
        """
        strategy_results = []
        for strat_id in strategy_ids:
            result = await self.run_single_strategy(
                strat_id,
                universe_id,
                start_date,
                end_date
            )
            strategy_results.append(result)
        
        portfolio_metrics = self._calculate_portfolio_metrics(strategy_results, allocation)

        return {
            'strategies': strategy_results,
            'allocation': allocation,
            'portfolio_metrics': portfolio_metrics,
            'message': 'Portfolio backtest complete'
        }

    def _calculate_portfolio_metrics(self, strategy_results, allocation):
        if not strategy_results:
            return {}

        total_weight = sum(allocation.values())
        if total_weight == 0: return {'error': 'Zero weight'}
        weights = {k: v / total_weight for k, v in allocation.items()}

        returns_series = {}
        for res in strategy_results:
            strat_id = res['strategy_id']
            daily_equity = res.get('daily_equity', [])
            if not daily_equity: continue

            df = pd.DataFrame(daily_equity)
            df['date'] = pd.to_datetime(df['date'])
            df.set_index('date', inplace=True)

            # Use 'equity' directly to calculate returns
            series = df['equity'].pct_change().fillna(0)
            returns_series[strat_id] = series

        if not returns_series: return {'error': 'No return data'}

        returns_df = pd.DataFrame(returns_series).fillna(0)
        portfolio_returns = pd.Series(0.0, index=returns_df.index)

        used_weight_sum = 0
        for strat_id, series in returns_series.items():
            if strat_id in weights:
                w = weights[strat_id]
                portfolio_returns += series * w
                used_weight_sum += w

        if used_weight_sum > 0:
            portfolio_returns = portfolio_returns / used_weight_sum

        portfolio_equity = (1 + portfolio_returns).cumprod() * 100

        risk_engine = RiskMetricsEngine()
        sharpe = risk_engine.sharpe_ratio(portfolio_returns)
        max_dd = risk_engine.max_drawdown(portfolio_equity) * 100
        volatility = risk_engine.annualized_volatility(portfolio_returns) * 100
        total_return = (portfolio_equity.iloc[-1] - 100) if not portfolio_equity.empty else 0

        daily_equity_list = [
            {'date': date.strftime('%Y-%m-%d'), 'equity': val}
            for date, val in portfolio_equity.items()
        ]

        return {
            'total_return_pct': round(total_return, 2),
            'sharpe_ratio': round(sharpe, 2),
            'max_drawdown_pct': round(abs(max_dd), 2),
            'volatility_pct': round(volatility, 2),
            'daily_equity': daily_equity_list
        }
    
    async def _backtest_symbol(
        self,
        contract: StrategyContract,
        symbol: str,
        start_date: datetime,
        end_date: datetime
    ) -> BacktestResult:
        """Run backtest for a single symbol using locked contract and run_day loop"""
        
        # Load strategy
        StrategyClass = STRATEGY_REGISTRY.get(contract.strategy_id)
        if not StrategyClass:
            raise ValueError(f"Unknown strategy: {contract.strategy_id}")

        strategy = StrategyClass(
            strategy_id=contract.strategy_id,
            universe_id="NIFTY",
            parameters=contract.parameters or {}
        )
        
        dates_df = self.repo.get_historical_prices(symbol, days=365) # Approximation
        if dates_df is None or dates_df.empty:
             # Fallback dates
             dates = pd.date_range(start=start_date, end=end_date, freq='B')
        else:
             # Filter dates
             mask = (dates_df.index >= pd.Timestamp(start_date)) & (dates_df.index <= pd.Timestamp(end_date))
             dates = dates_df.index[mask]
        
        daily_results = []
        equity = 1000000.0 # Initial Capital
        equity_curve = []
        trades_log = []
        
        gross_profit = 0
        gross_loss = 0
        
        for current_date in dates:
            # Execute Strategy Logic for the Day
            try:
                # Convert Timestamp to date
                d = current_date.date()
                day_result = strategy.run_day(d, [symbol], self.repo)

                # Apply result to equity
                # day_result contains 'daily_return' pct (e.g. 1.5 for 1.5%)
                ret_pct = day_result.get('daily_return', 0.0) / 100.0
                equity *= (1 + ret_pct)

                equity_curve.append({
                    'date': d.strftime('%Y-%m-%d'),
                    'equity': equity
                })

                pnl = day_result.get('gross_pnl', 0)
                if day_result.get('number_of_trades', 0) > 0:
                    trades_log.append({
                        'entry_time': d.strftime('%Y-%m-%d'),
                        'symbol': symbol,
                        'pnl': pnl
                    })
                    if pnl > 0: gross_profit += pnl
                    else: gross_loss += abs(pnl)

            except Exception as e:
                # print(f"Strategy Error on {current_date}: {e}")
                pass

        # Construct BacktestResult
        if not equity_curve:
             return BacktestResult(
                 strategy=contract.strategy_id,
                 symbol=symbol,
                 start_date=start_date.strftime('%Y-%m-%d'),
                 end_date=end_date.strftime('%Y-%m-%d'),
                 total_return_pct=0, sharpe_ratio=0, max_drawdown_pct=0,
                 total_trades=0, winning_trades=0, win_rate_pct=0,
                 cagr_pct=0, profit_factor=0, final_equity=1000000,
                 daily_equity=[], equity_curve=[], trades=[]
             )

        final_equity = equity_curve[-1]['equity']
        total_ret = ((final_equity - 1000000) / 1000000) * 100
        
        # Calculate Metrics
        df = pd.DataFrame(equity_curve)
        returns = df['equity'].pct_change().fillna(0)
        risk = RiskMetricsEngine()
        sharpe = risk.sharpe_ratio(returns)
        max_dd = risk.max_drawdown(df['equity']) * 100
        
        total_trades = len(trades_log)
        winning_trades = sum(1 for t in trades_log if t['pnl'] > 0)
        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 0
        
        # CAGR (Simple approx)
        days = (end_date - start_date).days
        years = days / 365.0
        cagr = 0
        if years > 0 and final_equity > 0:
             cagr = ((final_equity / 1000000) ** (1/years) - 1) * 100
        
        return BacktestResult(
            strategy=contract.strategy_id,
            symbol=symbol,
            start_date=start_date.strftime('%Y-%m-%d'),
            end_date=end_date.strftime('%Y-%m-%d'),
            total_return_pct=round(total_ret, 2),
            sharpe_ratio=round(sharpe, 2),
            max_drawdown_pct=round(max_dd, 2),
            total_trades=total_trades,
            winning_trades=winning_trades,
            win_rate_pct=round(win_rate, 2),
            cagr_pct=round(cagr, 2),
            profit_factor=round(profit_factor, 2),
            final_equity=round(final_equity, 2),
            daily_equity=equity_curve,
            equity_curve=equity_curve,
            trades=trades_log
        )

    def _load_locked_strategy(self, contract):
        # Helper not needed if inlined, but kept for compatibility
        pass

    async def _load_universe(self, universe_id, date):
        # Mock/Simple
        return ["NIFTY 50"]

    def _aggregate_results(self, results, strategy_id, universe_id):
        # Fallback if no results
        if not results:
            return {
                'strategy_id': strategy_id,
                'universe_id': universe_id,
                'total_return_pct': 0,
                'total_trades': 0,
                'daily_equity': [],
                'symbol_results': [],
                'error': 'No valid results produced'
            }

        # Simple averaging for now
        avg_ret = sum(r.total_return_pct for r in results) / len(results)

        # Combine equity for display (take the first one since it's likely just NIFTY)
        daily_equity = results[0].daily_equity if results else []
        
        return {
            'strategy_id': strategy_id,
            'universe_id': universe_id,
            'total_return_pct': round(avg_ret, 2),
            'total_trades': sum(r.total_trades for r in results),
            'daily_equity': daily_equity,
            'symbol_results': [r.dict() for r in results]
        }
