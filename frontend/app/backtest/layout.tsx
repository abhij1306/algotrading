import { Outlet } from 'react-router';

export default function BacktestLayout() {
  return (
    <div className="flex h-screen flex-col overflow-hidden bg-background">
      <title>Backtest | SmartTrader</title>
      <Outlet />
    </div>
  );
}
