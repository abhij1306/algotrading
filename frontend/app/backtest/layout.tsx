import { ReactNode } from 'react';

export const metadata = {
  title: 'Backtest | SmartTrader',
  description: 'Backtest trading strategies on stocks, options, and index universes',
};

export default function BacktestLayout({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen bg-background">
      {children}
    </div>
  );
}
