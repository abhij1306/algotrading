/**
 * Light/dark theme store.
 *
 * - The user's explicit choice is persisted in localStorage; with no choice the theme follows
 *   the system preference (and keeps following it while it changes).
 * - `index.html` runs an inline copy of the initial read before first paint so the page never
 *   flashes the wrong theme; keep `THEME_STORAGE_KEY` and the values in sync with it. That copy
 *   also moves a choice saved under the old `smarttrader-theme` key, once.
 * - React reads the store through `useSyncExternalStore`; there are no effects.
 */
import { useSyncExternalStore } from "react";

export type Theme = "light" | "dark";

export const THEME_STORAGE_KEY = "hat-theme";
const DARK_SCHEME_QUERY = "(prefers-color-scheme: dark)";

function isTheme(value: unknown): value is Theme {
  return value === "light" || value === "dark";
}

/** The persisted choice, or null when there is none or storage is unavailable. */
function readStoredTheme(): Theme | null {
  try {
    const stored = globalThis.localStorage.getItem(THEME_STORAGE_KEY);
    return isTheme(stored) ? stored : null;
  } catch {
    return null;
  }
}

function writeStoredTheme(theme: Theme): void {
  try {
    globalThis.localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // Storage blocked or full: the choice still applies for this page view.
  }
}

function darkSchemeQuery(): MediaQueryList | null {
  return typeof globalThis.matchMedia === "function"
    ? globalThis.matchMedia(DARK_SCHEME_QUERY)
    : null;
}

function applyTheme(theme: Theme): void {
  document.documentElement.classList.toggle("dark", theme === "dark");
}

export function createThemeStore() {
  const media = darkSchemeQuery();
  let chosen = readStoredTheme();
  let theme: Theme = chosen ?? (media?.matches ? "dark" : "light");
  const listeners = new Set<() => void>();
  applyTheme(theme);

  function update(next: Theme) {
    if (next === theme) return;
    theme = next;
    applyTheme(next);
    for (const listener of listeners) listener();
  }

  function onSystemChange(event: MediaQueryListEvent) {
    if (chosen === null) update(event.matches ? "dark" : "light");
  }

  function setTheme(next: Theme) {
    chosen = next;
    writeStoredTheme(next);
    update(next);
  }

  function subscribe(listener: () => void): () => void {
    listeners.add(listener);
    if (listeners.size === 1) media?.addEventListener("change", onSystemChange);
    return () => {
      listeners.delete(listener);
      if (listeners.size === 0) media?.removeEventListener("change", onSystemChange);
    };
  }

  return {
    subscribe,
    getTheme: (): Theme => theme,
    setTheme,
    toggleTheme: () => setTheme(theme === "dark" ? "light" : "dark"),
  };
}

export type ThemeStore = ReturnType<typeof createThemeStore>;

export const themeStore: ThemeStore = createThemeStore();

/** The current theme and a toggle that persists the new choice. */
export function useTheme(store: ThemeStore = themeStore) {
  const theme = useSyncExternalStore(store.subscribe, store.getTheme);
  return { theme, isDark: theme === "dark", toggleTheme: store.toggleTheme };
}
