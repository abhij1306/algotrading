import { describe, expect, it } from "vite-plus/test";
import { render, screen } from "@testing-library/react";
import { Button } from "../button";

describe("Button", () => {
  it("is a plain button by default, so it never submits a form", () => {
    render(<Button>Go</Button>);
    expect(screen.getByRole("button", { name: "Go" })).toHaveAttribute("type", "button");
  });

  it("uses the accent token for the primary variant", () => {
    render(<Button variant="primary">Primary</Button>);
    expect(screen.getByRole("button", { name: "Primary" })).toHaveClass("bg-accent");
  });

  it("can be disabled", () => {
    render(<Button disabled>Disabled</Button>);
    expect(screen.getByRole("button", { name: "Disabled" })).toBeDisabled();
  });
});
