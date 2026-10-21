import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, beforeAll, describe, expect, it, vi } from "vite-plus/test";
import { createQueryClient } from "@/lib/queries/query-client";

// The client reads VITE_API_URL at import time; node cannot build a Request from a relative URL.
vi.stubEnv("VITE_API_URL", "http://api.test");
const { CommandPalette, useCommandPalette } = await import("../CommandPalette");

const WINDOWS = { platform: "Win32", userAgentData: { platform: "Windows" } };
const MAC = { platform: "MacIntel", userAgentData: undefined };

function setPlatform({ platform, userAgentData }: { platform: string; userAgentData?: object }) {
  Object.defineProperty(globalThis.navigator, "platform", { value: platform, configurable: true });
  Object.defineProperty(globalThis.navigator, "userAgentData", {
    value: userAgentData,
    configurable: true,
  });
}

function LocationProbe() {
  const location = useLocation();
  return <output aria-label="location">{location.pathname + location.search}</output>;
}

function Shell() {
  const { isOpen, close } = useCommandPalette();
  return <CommandPalette isOpen={isOpen} onClose={close} />;
}

function renderWithProviders(ui: React.ReactNode) {
  const client = createQueryClient();
  client.setDefaultOptions({ queries: { retry: false } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        {ui}
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const fetchMock = vi.fn<(request: Request) => Promise<Response>>();

beforeAll(() => {
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  fetchMock.mockReset();
  setPlatform(WINDOWS);
});

describe("CommandPalette shortcut hints", () => {
  it("shows Ctrl shortcuts on Windows", () => {
    setPlatform(WINDOWS);
    renderWithProviders(<CommandPalette isOpen onClose={vi.fn()} />);

    expect(screen.getByText("Ctrl+K Open")).toBeInTheDocument();
    expect(screen.getByText("Ctrl+1")).toBeInTheDocument();
    expect(screen.queryByText(/⌘/)).not.toBeInTheDocument();
  });

  it("shows ⌘ shortcuts on macOS", () => {
    setPlatform(MAC);
    renderWithProviders(<CommandPalette isOpen onClose={vi.fn()} />);

    expect(screen.getByText("⌘K Open")).toBeInTheDocument();
    expect(screen.getByText("⌘1")).toBeInTheDocument();
    expect(screen.queryByText(/Ctrl/)).not.toBeInTheDocument();
  });
});

describe("useCommandPalette keyboard shortcut", () => {
  it("toggles the palette with Ctrl+K on Windows and ignores ⌘K", () => {
    setPlatform(WINDOWS);
    renderWithProviders(<Shell />);

    fireEvent.keyDown(globalThis.window, { key: "k", metaKey: true });
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();

    fireEvent.keyDown(globalThis.window, { key: "k", ctrlKey: true });
    expect(screen.getByRole("textbox")).toBeInTheDocument();

    fireEvent.keyDown(globalThis.window, { key: "K", ctrlKey: true });
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("ignores auto-repeat while the shortcut is held", () => {
    setPlatform(WINDOWS);
    renderWithProviders(<Shell />);

    fireEvent.keyDown(globalThis.window, { key: "k", ctrlKey: true });
    fireEvent.keyDown(globalThis.window, { key: "k", ctrlKey: true, repeat: true });
    expect(screen.getByRole("textbox")).toBeInTheDocument();

    fireEvent.keyDown(globalThis.window, { key: "k", ctrlKey: true });
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("toggles the palette with ⌘K on macOS and ignores Ctrl+K", () => {
    setPlatform(MAC);
    renderWithProviders(<Shell />);

    fireEvent.keyDown(globalThis.window, { key: "k", ctrlKey: true });
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();

    fireEvent.keyDown(globalThis.window, { key: "k", metaKey: true });
    expect(screen.getByRole("textbox")).toBeInTheDocument();

    fireEvent.keyDown(globalThis.window, { key: "k", metaKey: true });
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });
});

describe("CommandPalette commands", () => {
  it("lists the navigation commands and no symbol search", () => {
    renderWithProviders(<CommandPalette isOpen onClose={vi.fn()} />);

    expect(screen.getByText("Dashboard")).toBeInTheDocument();
    expect(screen.queryByText(/terminal/i)).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("runs the highlighted command on Enter", async () => {
    const onClose = vi.fn();
    renderWithProviders(<CommandPalette isOpen onClose={onClose} />);

    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });

    await waitFor(() => expect(screen.getByLabelText("location")).toHaveTextContent("/dashboard"));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("filters the commands as you type and says so when nothing matches", () => {
    renderWithProviders(<CommandPalette isOpen onClose={vi.fn()} />);

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "zzzz" } });

    expect(screen.getByText(/No results found for/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
