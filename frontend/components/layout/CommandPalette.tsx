import { useCallback, useMemo, useState, useSyncExternalStore, type KeyboardEvent } from "react";
import { useNavigate } from "react-router";
import { Search, TrendingUp, BarChart3, Activity, Settings, Home, LineChart } from "lucide-react";
import { formatInr, formatSignedPercent, roundsNegative } from "@/lib/format";
import { cn } from "@/lib/utils";
import { debounce } from "@/lib/debounce";
import { focusOnMount } from "@/lib/focus";
import { formatShortcut, isModifierShortcut } from "@/lib/platform";
import { useSymbolSearch, type SearchResultItem } from "@/lib/queries/terminal";
import { useKnownQuotes } from "@/lib/realtime/hooks";
import type { Quote } from "@/lib/realtime/quote-store";

interface Command {
  id: string;
  title: string;
  subtitle?: string;
  shortcut?: string;
  icon: React.ReactNode;
  action: () => void;
  category: string;
}

interface CommandPaletteProps {
  isOpen: boolean;
  onClose: () => void;
}

const MAX_SYMBOL_RESULTS = 5;
const MIN_SEARCH_LENGTH = 2;
const SEARCH_DEBOUNCE_MS = 250;
const NO_SYMBOLS: readonly SearchResultItem[] = [];

function terminalPath(symbol: string): string {
  return `/terminal?symbol=${encodeURIComponent(symbol)}`;
}

/** Debounced symbol search against the backend; results show from two typed characters. */
function usePaletteSymbolSearch() {
  const [text, setText] = useState("");
  const [term, setTerm] = useState("");
  const debouncedSetTerm = useMemo(
    () =>
      debounce(
        (value: unknown) => setTerm(typeof value === "string" ? value : ""),
        SEARCH_DEBOUNCE_MS,
      ),
    [],
  );
  // Ref callback on the search input: focus it on mount and drop a pending search on unmount.
  const inputRef = useCallback(
    (element: HTMLInputElement | null) => {
      focusOnMount(element);
      return () => debouncedSetTerm.cancel();
    },
    [debouncedSetTerm],
  );
  const active = text.trim().length >= MIN_SEARCH_LENGTH;
  const search = useSymbolSearch(term);
  // Only list results for the text as typed now: never a previous query's (pending debounce or
  // placeholder data), so Enter cannot open a symbol unrelated to the current input.
  const current = active && term === text.trim() && !search.isPlaceholderData;
  const symbols = current ? (search.data ?? NO_SYMBOLS).slice(0, MAX_SYMBOL_RESULTS) : NO_SYMBOLS;
  return {
    text,
    symbols,
    searching: active && (!current || search.isFetching),
    inputRef,
    onTextChange: (value: string) => {
      setText(value);
      debouncedSetTerm(value.trim());
    },
  };
}

/** Live price and day change, shown only when the quote store already has them. */
function LiveQuote({ quote }: Readonly<{ quote: Quote | undefined }>) {
  if (quote?.ltp === undefined) return null;
  const { ltp, changePct } = quote;
  return (
    <div className="text-right">
      <div className="font-medium tabular-nums">{formatInr(ltp)}</div>
      {changePct === undefined ? null : (
        <div
          className={cn(
            "text-xs tabular-nums",
            roundsNegative(changePct) ? "text-loss" : "text-profit",
          )}
        >
          {formatSignedPercent(changePct)}
        </div>
      )}
    </div>
  );
}

/** Mounts the dialog only while open, so its search state starts fresh on every open. */
export function CommandPalette({ isOpen, onClose }: Readonly<CommandPaletteProps>) {
  if (!isOpen) return null;
  return <CommandPaletteDialog onClose={onClose} />;
}

function CommandPaletteDialog({ onClose }: Readonly<Pick<CommandPaletteProps, "onClose">>) {
  const navigate = useNavigate();
  const [selectedIndex, setSelectedIndex] = useState(0);
  const { text: query, symbols, searching, inputRef, onTextChange } = usePaletteSymbolSearch();
  const quotes = useKnownQuotes();

  const commands = useMemo((): Command[] => {
    const all: Command[] = [
      {
        id: "nav-dashboard",
        title: "Dashboard",
        subtitle: "View your portfolio and market overview",
        shortcut: formatShortcut("1"),
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
        shortcut: formatShortcut("2"),
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
        shortcut: formatShortcut("3"),
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
        shortcut: formatShortcut("4"),
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
        shortcut: formatShortcut("5"),
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
        shortcut: formatShortcut(","),
        icon: <Settings className="w-4 h-4" />,
        action: () => {
          onClose();
        },
        category: "Preferences",
      },
    ];

    if (!query) return all;

    const lowerQuery = query.toLowerCase();
    return all.filter(
      (cmd) =>
        cmd.title.toLowerCase().includes(lowerQuery) ||
        cmd.subtitle?.toLowerCase().includes(lowerQuery) ||
        cmd.category.toLowerCase().includes(lowerQuery),
    );
  }, [query, navigate, onClose]);

  const totalItems = symbols.length + commands.length;
  const activeIndex = totalItems === 0 ? -1 : Math.min(selectedIndex, totalItems - 1);

  const selectSymbol = (symbol: string) => {
    void navigate(terminalPath(symbol));
    onClose();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        if (totalItems > 0) setSelectedIndex((activeIndex + 1) % totalItems);
        break;
      case "ArrowUp":
        event.preventDefault();
        if (totalItems > 0) setSelectedIndex((activeIndex - 1 + totalItems) % totalItems);
        break;
      case "Enter": {
        event.preventDefault();
        const symbol = activeIndex >= 0 ? symbols.at(activeIndex) : undefined;
        if (symbol) selectSymbol(symbol.symbol);
        else if (activeIndex >= 0) commands.at(activeIndex - symbols.length)?.action();
        break;
      }
      case "Escape":
        onClose();
        break;
      default:
        break;
    }
  };

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
              onTextChange(e.target.value);
              setSelectedIndex(0);
            }}
            onKeyDown={handleKeyDown}
            ref={inputRef}
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
                      key={`${symbol.symbol}-${symbol.type ?? ""}`}
                      onClick={() => selectSymbol(symbol.symbol)}
                      className={cn(
                        "w-full flex items-center gap-3 px-4 py-3 cursor-pointer transition-all",
                        activeIndex === index && "bg-background-tertiary",
                      )}
                    >
                      <div className="w-8 h-8 flex items-center justify-center rounded-sm bg-background-tertiary">
                        <TrendingUp className="w-4 h-4 text-primary" />
                      </div>
                      <div className="flex-1 text-left">
                        <div className="font-medium">{symbol.symbol}</div>
                        <div className="text-xs text-foreground-secondary">{symbol.name}</div>
                      </div>
                      <LiveQuote quote={quotes[symbol.symbol]} />
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
                            activeIndex === actualIndex && "bg-background-tertiary",
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
              {searching ? "Searching..." : <>No results found for &quot;{query}&quot;</>}
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
            <span>{formatShortcut("K")} Open</span>
            <span>ESC Close</span>
          </div>
        </div>
      </div>
    </>
  );
}

/** Open state shared by the shell; the platform's ⌘K / Ctrl+K toggles it while anything listens. */
function createCommandPaletteStore() {
  let isOpen = false;
  const listeners = new Set<() => void>();

  function setOpen(next: boolean) {
    if (next === isOpen) return;
    isOpen = next;
    for (const listener of listeners) listener();
  }

  function onKeyDown(event: globalThis.KeyboardEvent) {
    if (event.repeat || !isModifierShortcut(event, "k")) return;
    event.preventDefault();
    setOpen(!isOpen);
  }

  function subscribe(listener: () => void): () => void {
    listeners.add(listener);
    if (listeners.size === 1) globalThis.addEventListener("keydown", onKeyDown);
    return () => {
      listeners.delete(listener);
      if (listeners.size === 0) globalThis.removeEventListener("keydown", onKeyDown);
    };
  }

  return {
    subscribe,
    getIsOpen: () => isOpen,
    open: () => setOpen(true),
    close: () => setOpen(false),
  };
}

const commandPaletteStore = createCommandPaletteStore();

export function useCommandPalette() {
  const isOpen = useSyncExternalStore(commandPaletteStore.subscribe, commandPaletteStore.getIsOpen);
  return { isOpen, open: commandPaletteStore.open, close: commandPaletteStore.close };
}
