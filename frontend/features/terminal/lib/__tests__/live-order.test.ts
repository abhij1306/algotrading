import { describe, expect, it } from "vite-plus/test";
import type { RiskCheck } from "@/lib/queries/terminal";
import { liveOrderRows, riskCheckBody, riskPanelText } from "../live-order";
import { orderDraft, type TicketInput } from "../order";

function draft(overrides: Partial<TicketInput> = {}) {
  return orderDraft({
    mode: "LIVE",
    symbol: "SBIN",
    side: "BUY",
    quantity: 10,
    lots: null,
    orderType: "LIMIT",
    price: 812.5,
    trigger: 0,
    riskOverrideReason: "",
    ...overrides,
  });
}

describe("liveOrderRows", () => {
  it("lists every field of an equity LIMIT order", () => {
    expect(liveOrderRows(draft())).toEqual([
      ["Mode", "LIVE"],
      ["Side", "BUY"],
      ["Symbol", "SBIN"],
      ["Instrument", "EQ"],
      ["Quantity", "10"],
      ["Order type", "LIMIT"],
      ["Price", "812.50"],
      ["Trigger", "None"],
      ["Product", "INTRADAY"],
      ["Risk override reason", "None"],
    ]);
  });

  it("shows an option's lots, the market price and the reason", () => {
    const rows = liveOrderRows(
      draft({
        symbol: "NIFTY26OCT25000PE",
        side: "SELL",
        quantity: 150,
        lots: { count: 2, size: 75 },
        orderType: "MARKET",
        riskOverrideReason: " hedge ",
      }),
    );
    expect(rows).toEqual([
      ["Mode", "LIVE"],
      ["Side", "SELL"],
      ["Symbol", "NIFTY26OCT25000PE"],
      ["Instrument", "PE"],
      ["Quantity", "150 (2 lots of 75)"],
      ["Order type", "MARKET"],
      ["Price", "Market"],
      ["Trigger", "None"],
      ["Product", "INTRADAY"],
      ["Risk override reason", "hedge"],
    ]);
  });

  it.each([
    [1234.5, 1200, "1,234.50", "1,200.00"],
    [812.125, 812.0001, "812.125", "812.0001"],
  ])("shows SL price %s and trigger %s exactly", (price, trigger, shownPrice, shownTrigger) => {
    const rows = new Map(liveOrderRows(draft({ orderType: "SL", price, trigger })));
    expect([rows.get("Price"), rows.get("Trigger")]).toEqual([shownPrice, shownTrigger]);
  });
});

describe("riskCheckBody", () => {
  it("checks the order's own fields", () => {
    expect(riskCheckBody(draft({ orderType: "SL", trigger: 800 }))).toEqual({
      symbol: "SBIN",
      side: "BUY",
      quantity: 10,
      product: "INTRADAY",
      type: "SL",
      price: 812.5,
      instrument_type: "EQ",
    });
  });
});

describe("riskPanelText", () => {
  const check = (status: RiskCheck["status"], message = "Too big"): RiskCheck => ({
    status,
    message,
    warnings: [],
  });

  it.each([
    ["pending", { isError: false, data: undefined }, false, "Checking risk..."],
    [
      "unavailable",
      { isError: true, data: undefined },
      false,
      "Risk check unavailable; the order is checked again when placed.",
    ],
    ["passed", { isError: false, data: check("PASS") }, false, "All risk checks passed."],
    ["failed", { isError: false, data: check("FAIL") }, true, "Will be rejected: Too big"],
    [
      "warned without a reason",
      { isError: false, data: check("WARNING") },
      false,
      "Risk warnings: add a risk override reason, or the order will be rejected.",
    ],
    [
      "warned with a reason",
      { isError: false, data: check("WARNING") },
      true,
      "Risk warnings, overridden by your reason:",
    ],
    [
      "an answer kept through a refetch error",
      { isError: true, data: check("PASS") },
      false,
      "All risk checks passed.",
    ],
  ])("%s", (_name, state, hasReason, expected) => {
    expect(riskPanelText(state, hasReason)).toBe(expected);
  });
});
