import { useMemo, useState, useSyncExternalStore, type KeyboardEvent } from "react";
import { useNavigate } from "react-router";
import { Search, Settings, Home } from "lucide-react";
import { cn } from "@/lib/utils";
import { focusOnMount } from "@/lib/focus";
import { formatShortcut, isModifierShortcut } from "@/lib/platform";

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

/** Mounts the dialog only while open, so its search state starts fresh on every open. */
export function CommandPalette({ isOpen, onClose }: Readonly<CommandPaletteProps>) {
  if (!isOpen) return null;
  return <CommandPaletteDialog onClose={onClose} />;
}

function CommandPaletteDialog({ onClose }: Readonly<Pick<CommandPaletteProps, "onClose">>) {
  const navigate = useNavigate();
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [query, setQuery] = useState("");

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

  const totalItems = commands.length;
  const activeIndex = totalItems === 0 ? -1 : Math.min(selectedIndex, totalItems - 1);

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
      case "Enter":
        event.preventDefault();
        if (activeIndex >= 0) commands.at(activeIndex)?.action();
        break;
      case "Escape":
        onClose();
        break;
      default:
        break;
    }
  };

  const hasResults = commands.length > 0;

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
            onKeyDown={handleKeyDown}
            ref={focusOnMount}
            placeholder="Search for apps or commands..."
            className="flex-1 bg-transparent border-none outline-none text-lg py-4 text-foreground"
          />
        </div>

        {/* Results */}
        <div className="max-h-[400px] overflow-y-auto py-2">
          {hasResults ? (
            <>
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
                      const actualIndex = commands.indexOf(command);
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
