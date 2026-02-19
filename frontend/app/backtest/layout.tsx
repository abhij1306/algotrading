import { ReactNode } from 'react';

export const metadata = {
  title: 'Backtest | SmartTrader',
  description: 'Backtest trading strategies on stocks, options, and index universes',
};

export default function BacktestLayout({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-screen flex-col overflow-hidden bg-background">
      {children}
    </div>
  );
}
