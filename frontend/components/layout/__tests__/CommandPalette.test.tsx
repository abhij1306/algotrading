import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vite-plus/test";
import { CommandPalette } from "../CommandPalette";

function renderPalette() {
  render(
    <MemoryRouter>
      <CommandPalette isOpen onClose={vi.fn()} />
    </MemoryRouter>,
  );
}

describe("CommandPalette symbol results", () => {
  it("shows each symbol's day change as a signed percent", () => {
    renderPalette();

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Reliance" } });
    expect(screen.getByText("+1.20%")).toBeInTheDocument();

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "TCS" } });
    expect(screen.getByText("-0.50%")).toBeInTheDocument();
  });
});
