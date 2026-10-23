import { describe, expect, it } from "vite-plus/test";
import { formatNumber } from "../format";

type Missing = null | undefined;
const MISSING: ReadonlyArray<[string, number | Missing]> = [
  ["null", null],
  ["undefined", undefined],
  ["NaN", Number.NaN],
  ["Infinity", Number.POSITIVE_INFINITY],
  ["-Infinity", Number.NEGATIVE_INFINITY],
];

describe("missing values", () => {
  it.each(MISSING)("formatNumber renders %s as the placeholder", (_label, value) => {
    expect(formatNumber(value)).toBe("--");
  });
});

describe("formatNumber", () => {
  it.each([
    [0, 2, "0.00"],
    [-0, 2, "0.00"],
    [-0.001, 2, "0.00"],
    [1234.5, 2, "1,234.50"],
    [-1234.5, 2, "-1,234.50"],
    [12345678.912, 2, "1,23,45,678.91"],
    [1e12, 0, "10,00,00,00,00,000"],
    [0.12345, 4, "0.1235"],
  ])("formatNumber(%s, %s) is %s", (value, decimals, expected) => {
    expect(formatNumber(value, decimals)).toBe(expected);
  });
});
