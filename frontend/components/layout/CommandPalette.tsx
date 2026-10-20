import { useState, useEffect, useCallback, useMemo } from "react";
import { useNavigate } from "react-router";
import { Search, TrendingUp, BarChart3, Activity, Settings, Home, LineChart } from "lucide-react";
import { cn, formatPercentage } from "@/lib/utils";
import { focusOnMount } from "@/lib/focus";

interface Command {
  id: string;
  title: string;
  subtitle?: string;
  shortcut?: string;
  icon: React.ReactNode;
  action: () => void;
  category: string;
}

interface SymbolResult {
  symbol: string;
  name: string;
  exchange: string;
  price?: number;
  /** Day change in percent (e.g. 1.2 means +1.2%), not an absolute price difference. */
  changePercent: number;
}

interface CommandPaletteProps {
  isOpen: boolean;
  onClose: () => void;
}

const MOCK_SYMBOLS: SymbolResult[] = [
  {
    symbol: "RELIANCE",
    name: "Reliance Industries",
    exchange: "NSE",
    price: 2450.5,
    changePercent: 1.2,
  },
  {
    symbol: "TCS",
    name: "Tata Consultancy Services",
    exchange: "NSE",
    price: 3890.25,
    changePercent: -0.5,
  },
  { symbol: "INFY", name: "Infosys Ltd", exchange: "NSE", price: 1650.75, changePercent: 0.8 },
  { symbol: "HDFCBANK", name: "HDFC Bank", exchange: "NSE", price: 1420, changePercent: 0.3 },
  {
    symbol: "SBIN",
    name: "State Bank of India",
    exchange: "NSE",
    price: 675.5,
    changePercent: 1.5,
  },
  { symbol: "NIFTY50", name: "NIFTY 50 Index", exchange: "NSE", price: 22450, changePercent: 0.6 },
];

function filterSymbols(query: string): SymbolResult[] {
  if (query.length < 1) return [];
  const lowerQuery = query.toLowerCase();
  return MOCK_SYMBOLS.filter(
    (s) => s.symbol.toLowerCase().includes(lowerQuery) || s.name.toLowerCase().includes(lowerQuery),
  ).slice(0, 5);
}

/** Mounts the dialog only while open, so its search state starts fresh on every open. */
export function CommandPalette({ isOpen, onClose }: Readonly<CommandPaletteProps>) {
  if (!isOpen) return null;
  return <CommandPaletteDialog onClose={onClose} />;
}

function CommandPaletteDialog({ onClose }: Readonly<Pick<CommandPaletteProps, "onClose">>) {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);
  const symbols = useMemo(() => filterSymbols(query), [query]);

  const getCommands = useCallback((): Command[] => {
    const commands: Command[] = [
      {
        id: "nav-dashboard",
        title: "Dashboard",
        subtitle: "View your portfolio and market overview",
        shortcut: "⌘1",
        icon: <Home className="w-4 h-4" />,
        action: () => {
          void navigate("/dashboard");
          onClose();
        },
        category: "Navigation",
      },
      {
        id: "nav-terminal",
        title: "Market Terminal",
        subtitle: "Advanced charts and trading",
        shortcut: "⌘2",
        icon: <LineChart className="w-4 h-4" />,
        action: () => {
          void navigate("/terminal");
          onClose();
        },
        category: "Navigation",
      },
      {
        id: "nav-screener",
        title: "Stock Screener",
        subtitle: "Find stocks matching your criteria",
        shortcut: "⌘3",
        icon: <BarChart3 className="w-4 h-4" />,
        action: () => {
          void navigate("/screener");
          onClose();
        },
        category: "Navigation",
      },
      {
        id: "nav-backtest",
        title: "Backtest",
        subtitle: "Test your strategies",
        shortcut: "⌘4",
        icon: <Activity className="w-4 h-4" />,
        action: () => {
          void navigate("/backtest");
          onClose();
        },
        category: "Navigation",
      },
      {
        id: "nav-strategies",
        title: "Strategies",
        subtitle: "Run the VCP scanner and manage strategy signals",
        shortcut: "⌘5",
        icon: <TrendingUp className="w-4 h-4" />,
        action: () => {
          void navigate("/strategies");
          onClose();
        },
        category: "Navigation",
      },
      {
        id: "action-backtest",
        title: "Run Quick Backtest",
        subtitle: "Start a new backtest with default settings",
        icon: <Activity className="w-4 h-4" />,
        action: () => {
          void navigate("/backtest/new");
          onClose();
        },
        category: "Actions",
      },
      {
        id: "settings",
        title: "Settings",
        subtitle: "Manage your preferences",
        shortcut: "⌘,",
        icon: <Settings className="w-4 h-4" />,
        action: () => {
          onClose();
        },
        category: "Preferences",
      },
    ];

    if (!query) return commands;

    const lowerQuery = query.toLowerCase();
    return commands.filter(
      (cmd) =>
        cmd.title.toLowerCase().includes(lowerQuery) ||
        cmd.subtitle?.toLowerCase().includes(lowerQuery) ||
        cmd.category.toLowerCase().includes(lowerQuery),
    );
  }, [query, navigate, onClose]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      const commands = getCommands();
      const totalItems = commands.length + symbols.length;

      switch (e.key) {
        case "ArrowDown":
          e.preventDefault();
          setSelectedIndex((prev) => (prev + 1) % totalItems);
          break;
        case "ArrowUp":
          e.preventDefault();
          setSelectedIndex((prev) => (prev - 1 + totalItems) % totalItems);
          break;
        case "Enter":
          e.preventDefault();
          if (selectedIndex < symbols.length) {
            const symbol = symbols[selectedIndex];
            void navigate(`/terminal?symbol=${symbol.symbol}`);
            onClose();
          } else {
            const cmd = commands[selectedIndex - symbols.length];
            if (cmd) cmd.action();
          }
          break;
        case "Escape":
          onClose();
          break;
      }
    };

    globalThis.addEventListener("keydown", handleKeyDown);
    return () => globalThis.removeEventListener("keydown", handleKeyDown);
  }, [selectedIndex, symbols, getCommands, navigate, onClose]);

  const commands = getCommands();
  const hasResults = commands.length > 0 || symbols.length > 0;

  return (
    <>
      {/* Backdrop */}
      <button
        type="button"
        className="fixed inset-0 bg-black/40 backdrop-blur-sm z-[600] animate-fade-in"
        aria-label="Close command palette"
        tabIndex={-1}
        onClick={onClose}
      />

      {/* Command Palette */}
      <div className="fixed top-[20%] left-1/2 -translate-x-1/2 w-full max-w-[680px] z-[600] animate-scale-in overflow-hidden bg-background-secondary rounded-lg shadow-xl border border-border">
        {/* Search Input */}
        <div className="flex items-center gap-3 px-4 border-b border-border-subtle">
          <Search className="w-5 h-5 text-foreground-tertiary" />
          <input
            id="command-palette-input"
            type="text"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setSelectedIndex(0);
            }}
            ref={focusOnMount}
            placeholder="Search for apps, symbols, or commands..."
            className="flex-1 bg-transparent border-none outline-none text-lg py-4 text-foreground"
          />
        </div>

        {/* Results */}
        <div className="max-h-[400px] overflow-y-auto py-2">
          {hasResults ? (
            <>
              {/* Symbols Section */}
              {symbols.length > 0 ? (
                <div className="mb-2">
                  <div className="px-4 py-2 text-xs font-semibold uppercase tracking-wider text-foreground-tertiary">
                    Symbols
                  </div>
                  {symbols.map((symbol, index) => (
                    <button
                      key={symbol.symbol}
                      onClick={() => {
                        void navigate(`/terminal?symbol=${symbol.symbol}`);
                        onClose();
                      }}
                      className={cn(
                        "w-full flex items-center gap-3 px-4 py-3 cursor-pointer transition-all",
                        selectedIndex === index && "bg-background-tertiary",
                      )}
                    >
                      <div className="w-8 h-8 flex items-center justify-center rounded-sm bg-background-tertiary">
                        <TrendingUp className="w-4 h-4 text-primary" />
                      </div>
                      <div className="flex-1 text-left">
                        <div className="font-medium">{symbol.symbol}</div>
                        <div className="text-xs text-foreground-secondary">{symbol.name}</div>
                      </div>
                      <div className="text-right">
                        <div className="font-medium tabular-nums">
                          ₹{symbol.price?.toLocaleString()}
                        </div>
                        <div
                          className={cn(
                            "text-xs tabular-nums",
                            symbol.changePercent >= 0 ? "text-profit" : "text-loss",
                          )}
                        >
                          {formatPercentage(symbol.changePercent)}
                        </div>
                      </div>
                    </button>
                  ))}
                </div>
              ) : null}

              {/* Commands by Category */}
              {["Navigation", "Actions", "Preferences"].map((category) => {
                const categoryCommands = commands.filter((c) => c.category === category);
                if (categoryCommands.length === 0) return null;

                return (
                  <div key={category} className="mb-2">
                    <div className="px-4 py-2 text-xs font-semibold uppercase tracking-wider text-foreground-tertiary">
                      {category}
                    </div>
                    {categoryCommands.map((command) => {
                      const actualIndex = symbols.length + commands.indexOf(command);
                      return (
                        <button
                          key={command.id}
                          onClick={command.action}
                          className={cn(
                            "w-full flex items-center gap-3 px-4 py-3 cursor-pointer transition-all",
                            selectedIndex === actualIndex && "bg-background-tertiary",
                          )}
                        >
                          <div className="w-8 h-8 flex items-center justify-center rounded-sm bg-background-tertiary text-foreground-secondary">
                            {command.icon}
                          </div>
                          <div className="flex-1 text-left">
                            <div className="font-medium">{command.title}</div>
                            <div className="text-xs text-foreground-secondary">
                              {command.subtitle}
                            </div>
                          </div>
                          {command.shortcut && (
                            <kbd className="px-2 py-1 text-xs rounded-sm bg-background-tertiary text-foreground-secondary">
                              {command.shortcut}
                            </kbd>
                          )}
                        </button>
                      );
                    })}
                  </div>
                );
              })}
            </>
          ) : (
            <div className="px-4 py-8 text-center text-foreground-tertiary">
              No results found for &quot;{query}&quot;
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-4 py-2 flex items-center justify-between text-xs border-t border-border-subtle text-foreground-tertiary">
          <div className="flex items-center gap-4">
            <span>↑↓ Navigate</span>
            <span>↵ Select</span>
          </div>
          <div className="flex items-center gap-4">
            <span>⌘K Open</span>
            <span>ESC Close</span>
          </div>
        </div>
      </div>
    </>
  );
}

export function useCommandPalette() {
  const [isOpen, setIsOpen] = useState(false);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setIsOpen((prev) => !prev);
      }
    };

    globalThis.addEventListener("keydown", handleKeyDown);
    return () => globalThis.removeEventListener("keydown", handleKeyDown);
  }, []);

  return { isOpen, open: () => setIsOpen(true), close: () => setIsOpen(false) };
}
