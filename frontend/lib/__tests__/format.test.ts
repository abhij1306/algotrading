import { describe, expect, it } from "vite-plus/test";
import {
  formatCompactInr,
  formatCompactNumber,
  formatCrores,
  formatInr,
  formatNumber,
  formatPercent,
  formatSignedPercent,
  roundToDecimals,
  roundsNegative,
} from "../format";

type Missing = null | undefined;
const MISSING: ReadonlyArray<[string, number | Missing]> = [
  ["null", null],
  ["undefined", undefined],
  ["NaN", Number.NaN],
  ["Infinity", Number.POSITIVE_INFINITY],
  ["-Infinity", Number.NEGATIVE_INFINITY],
];

const FORMATTERS: ReadonlyArray<[string, (value: number | Missing) => string]> = [
  ["formatNumber", (value) => formatNumber(value)],
  ["formatInr", (value) => formatInr(value)],
  ["formatPercent", (value) => formatPercent(value)],
  ["formatSignedPercent", (value) => formatSignedPercent(value)],
  ["formatCompactInr", (value) => formatCompactInr(value)],
  ["formatCompactNumber", (value) => formatCompactNumber(value)],
  ["formatCrores", (value) => formatCrores(value)],
];

describe("missing values", () => {
  for (const [name, format] of FORMATTERS) {
    it.each(MISSING)(`${name} renders %s as the placeholder`, (_label, value) => {
      expect(format(value)).toBe("--");
    });
  }
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

describe("formatInr", () => {
  it.each([
    [0, 2, "₹0.00"],
    [-0, 2, "₹0.00"],
    [-0.004, 2, "₹0.00"],
    [1234.5, 2, "₹1,234.50"],
    [-1234.5, 2, "-₹1,234.50"],
    [12345678.9, 2, "₹1,23,45,678.90"],
    [99999.999, 2, "₹1,00,000.00"],
    [1500, 0, "₹1,500"],
  ])("formatInr(%s, %s) is %s", (value, decimals, expected) => {
    expect(formatInr(value, decimals)).toBe(expected);
  });
});

describe("formatPercent", () => {
  it.each([
    [12.345, 1, "12.3%"],
    [-4.5, 2, "-4.50%"],
    [1250, 1, "1,250.0%"],
    [-0.04, 1, "0.0%"],
    [0, 2, "0.00%"],
  ])("formats %s with %s decimals as %s", (value, decimals, expected) => {
    expect(formatPercent(value, decimals)).toBe(expected);
  });
});

describe("formatSignedPercent", () => {
  it.each([
    [0, 2, "+0.00%"],
    [-0, 2, "+0.00%"],
    [-0.001, 2, "+0.00%"],
    [1.234, 2, "+1.23%"],
    [-1.235, 2, "-1.24%"],
    [10.075, 2, "+10.08%"],
    [-10.075, 2, "-10.08%"],
    [-0.004, 2, "+0.00%"],
    [-0.005, 2, "-0.01%"],
    [-0.5, 2, "-0.50%"],
    [12.345, 1, "+12.3%"],
    [123456.7, 2, "+123456.70%"],
  ])("formatSignedPercent(%s, %s) is %s", (value, decimals, expected) => {
    expect(formatSignedPercent(value, decimals)).toBe(expected);
  });
});

describe("formatCompactInr", () => {
  it.each([
    [0, 2, "₹0"],
    [-0, 2, "₹0"],
    [999.5, 2, "₹999.5"],
    [45000, 2, "₹45,000"],
    [99999.994, 2, "₹99,999.99"],
    [100000, 2, "₹1.00L"],
    [1234567, 2, "₹12.35L"],
    [1234567, 1, "₹12.3L"],
    [10000000, 1, "₹1.0Cr"],
    [15000000, 2, "₹1.50Cr"],
    [-2500000, 1, "-₹25.0L"],
    [-500, 2, "-₹500"],
    [1e12, 2, "₹100000.00Cr"],
  ])("formatCompactInr(%s, %s) is %s", (value, decimals, expected) => {
    expect(formatCompactInr(value, decimals)).toBe(expected);
  });
});

describe("formatCompactNumber", () => {
  it.each([
    [0, "0"],
    [-0, "0"],
    [-0.4, "0"],
    [999.4, "999"],
    [999.6, "1,000"],
    [1000, "1.0K"],
    [56789, "56.8K"],
    [345678, "3.5L"],
    [12345678, "1.2Cr"],
    [-56789, "-56.8K"],
  ])("formatCompactNumber(%s) is %s", (value, expected) => {
    expect(formatCompactNumber(value)).toBe(expected);
  });
});

describe("formatCrores", () => {
  it.each([
    [0, "0 Cr"],
    [-0.3, "0 Cr"],
    [820.4, "820 Cr"],
    [45000, "45K Cr"],
    [1234567, "12L Cr"],
    [-45000, "-45K Cr"],
  ])("formatCrores(%s) is %s", (value, expected) => {
    expect(formatCrores(value)).toBe(expected);
  });
});

describe("roundToDecimals", () => {
  it.each([
    [1.005, 2, 1.01],
    [-1.005, 2, -1.01],
    [-0.001, 2, 0],
    [123.456789, 4, 123.4568],
    [10.075, 2, 10.08],
    [-10.075, 2, -10.08],
    [1.0049999, 2, 1],
    [1e21, 2, 1e21],
    [123456789012.345, 2, 123456789012.35],
    [1e20, 2, 1e20],
    [1e-7, 2, 0],
    [-0, 2, 0],
    [null, 2, 0],
    [undefined, 2, 0],
    [Number.NaN, 2, 0],
  ])("roundToDecimals(%s, %s) is %s", (value, decimals, expected) => {
    expect(Object.is(roundToDecimals(value, decimals), expected)).toBe(true);
  });
});

describe("roundsNegative agrees with the sign formatSignedPercent prints", () => {
  it.each([
    [-0.004, false, "+0.00%"],
    [-0.005, true, "-0.01%"],
    [-0, false, "+0.00%"],
    [0.004, false, "+0.00%"],
    [-1.5, true, "-1.50%"],
    [null, false, "--"],
  ])("%s: negative=%s, shown as %s", (value, negative, shown) => {
    expect(roundsNegative(value)).toBe(negative);
    expect(formatSignedPercent(value)).toBe(shown);
  });
});
