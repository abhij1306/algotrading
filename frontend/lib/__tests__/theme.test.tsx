import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vite-plus/test";
import { createThemeStore, useTheme } from "../theme";

function isDarkApplied(): boolean {
  return document.documentElement.classList.contains("dark");
}

/** A fresh page: no theme class on <html>, then the store initialises as it does on load. */
function reload() {
  document.documentElement.classList.remove("dark");
  return createThemeStore();
}

function stubSystemPrefersDark(prefersDark: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: prefersDark && query === "(prefers-color-scheme: dark)",
    media: query,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }));
}

beforeEach(() => {
  globalThis.localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("theme store", () => {
  it("follows the system preference when nothing was chosen", () => {
    stubSystemPrefersDark(true);
    expect(reload().getTheme()).toBe("dark");
    expect(isDarkApplied()).toBe(true);

    stubSystemPrefersDark(false);
    expect(reload().getTheme()).toBe("light");
    expect(isDarkApplied()).toBe(false);
  });

  it("keeps the chosen theme across a reload", () => {
    stubSystemPrefersDark(true);
    reload().setTheme("light");

    const afterReload = reload();
    expect(afterReload.getTheme()).toBe("light");
    expect(isDarkApplied()).toBe(false);

    afterReload.toggleTheme();
    expect(reload().getTheme()).toBe("dark");
    expect(isDarkApplied()).toBe(true);
  });

  it("still switches theme when storage throws", () => {
    stubSystemPrefersDark(false);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });

    const store = reload();
    expect(store.getTheme()).toBe("light");

    store.setTheme("dark");
    expect(store.getTheme()).toBe("dark");
    expect(isDarkApplied()).toBe(true);
  });
});

describe("useTheme", () => {
  it("re-renders and persists when toggled", () => {
    stubSystemPrefersDark(false);
    const store = reload();
    const { result } = renderHook(() => useTheme(store));
    expect(result.current.isDark).toBe(false);

    act(() => result.current.toggleTheme());

    expect(result.current.isDark).toBe(true);
    expect(isDarkApplied()).toBe(true);
    expect(reload().getTheme()).toBe("dark");
  });
});
