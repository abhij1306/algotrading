'use client';

import { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { Search, Bell, Moon, Sun, Command } from 'lucide-react';
import { cn } from '@/lib/utils';
import LoginButton from '@/components/LoginButton';

interface HeaderProps {
  onCommandPaletteOpen: () => void;
}

export function AppHeader({ onCommandPaletteOpen }: Readonly<HeaderProps>) {
  const router = useRouter();
  const [isDark, setIsDark] = useState(false);

  useEffect(() => {
    if (globalThis.window !== undefined) {
      setIsDark(document.documentElement.classList.contains('dark'));
    }
  }, []);

  const toggleTheme = () => {
    const html = document.documentElement;
    if (html.classList.contains('dark')) {
      html.classList.remove('dark');
      setIsDark(false);
    } else {
      html.classList.add('dark');
      setIsDark(true);
    }
  };

  const navItems = [
    { id: 'dashboard', label: 'Dashboard', path: '/dashboard' },
    { id: 'terminal', label: 'Terminal', path: '/terminal' },
    { id: 'screener', label: 'Screener', path: '/screener' },
    { id: 'backtest', label: 'Backtest', path: '/backtest' },
  ];

  const headerClasses = "h-14 flex items-center justify-between px-4 sticky top-0 z-50 bg-background border-b border-border-subtle";
  const navLeftClasses = "flex items-center gap-4";
  const navRightClasses = "flex items-center gap-1";

  return (
    <header className={headerClasses}>
      {/* Left Side */}
      <div className={navLeftClasses}>
        {/* Logo */}
        <button
          onClick={() => router.push('/dashboard')}
          className="flex items-center gap-2 hover:opacity-80 transition-opacity"
        >
          <div className="w-8 h-8 flex items-center justify-center rounded-md bg-gradient-to-br from-primary to-purple-600">
            <Command className="w-5 h-5 text-white" />
          </div>
          <span className="font-semibold text-lg hidden sm:block text-foreground">
            SmartTrader
          </span>
        </button>

        {/* Navigation Links - Desktop */}
        <nav className="hidden lg:flex items-center gap-1 ml-6">
          {navItems.map((item) => (
            <button
              key={item.id}
              onClick={() => router.push(item.path)}
              className="px-3 py-1.5 text-sm font-medium transition-all rounded-md text-foreground-secondary hover:text-foreground hover:bg-background-tertiary"
            >
              {item.label}
            </button>
          ))}
        </nav>
      </div>

      {/* Right Side */}
      <div className={navRightClasses}>
        {/* Search Trigger */}
        <button
          onClick={onCommandPaletteOpen}
          className="flex items-center gap-2 px-3 py-1.5 mr-2 rounded-lg transition-all cursor-pointer select-none bg-background-secondary text-foreground-secondary border border-border hover:bg-background-tertiary"
        >
          <Search className="w-4 h-4" />
          <span className="hidden sm:inline">Search...</span>
          <kbd className="hidden md:inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-xxs font-medium bg-background-tertiary">
            <span>⌘</span><span>K</span>
          </kbd>
        </button>

        {/* Theme Toggle */}
        <button
          onClick={toggleTheme}
          className="p-2 rounded-lg transition-all text-foreground-secondary hover:bg-background-tertiary"
          title={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
        >
          {isDark ? <Sun className="w-5 h-5" /> : <Moon className="w-5 h-5" />}
        </button>

        {/* Notifications */}
        <button className="relative p-2 rounded-lg transition-all text-foreground-secondary hover:bg-background-tertiary">
          <Bell className="w-5 h-5" />
          <span className="absolute top-1.5 right-1.5 w-2 h-2 rounded-full bg-loss" />
        </button>

        {/* Fyers Login Button */}
        <div className="ml-2 pl-2 border-l border-border-subtle">
          <LoginButton />
        </div>
      </div>
    </header>
  );
}

// Mobile Navigation
export function MobileNav() {
  const router = useRouter();
  const [activeTab, setActiveTab] = useState('dashboard');

  const tabs = [
    { id: 'dashboard', label: 'Home', icon: Command, path: '/dashboard' },
    { id: 'terminal', label: 'Market', icon: Search, path: '/terminal' },
    { id: 'screener', label: 'Screen', icon: Search, path: '/screener' },
    { id: 'backtest', label: 'Test', icon: Bell, path: '/backtest' },
  ];

  return (
    <nav className="lg:hidden fixed bottom-0 left-0 right-0 h-16 flex items-center justify-around px-4 z-50 bg-background border-t border-border-subtle">
      {tabs.map((tab) => {
        const Icon = tab.icon;
        const isActive = activeTab === tab.id;

        return (
          <button
            key={tab.id}
            onClick={() => {
              setActiveTab(tab.id);
              router.push(tab.path);
            }}
            className={cn(
              'flex flex-col items-center gap-1 px-4 py-2 rounded-lg transition-all',
              isActive ? 'font-medium text-primary' : 'text-foreground-tertiary'
            )}
          >
            <Icon className="w-5 h-5" />
            <span className="text-xxs">{tab.label}</span>
          </button>
        );
      })}
    </nav>
  );
}
