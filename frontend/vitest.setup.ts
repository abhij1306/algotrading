import * as matchers from "@testing-library/jest-dom/matchers";
import { cleanup } from "@testing-library/react";
import { afterEach, expect } from "vite-plus/test";

expect.extend(matchers);

// jsdom has <dialog> but not its modal API: open and close it so modal dialogs render in tests.
for (const [name, open] of [
  ["showModal", true],
  ["close", false],
] as const) {
  if (!(name in HTMLDialogElement.prototype)) {
    Object.defineProperty(HTMLDialogElement.prototype, name, {
      configurable: true,
      value(this: HTMLDialogElement) {
        this.open = open;
      },
    });
  }
}
afterEach(cleanup);
