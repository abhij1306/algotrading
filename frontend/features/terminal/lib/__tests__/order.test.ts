import { describe, expect, it } from "vite-plus/test";
import type { PlaceOrderResult } from "@/lib/queries/terminal";
import {
  inferInstrumentType,
  isStopOrder,
  optionLotSize,
  orderBody,
  orderDraft,
  orderInputError,
  orderResultMessage,
  type TicketInput,
} from "../order";
import type { OrderType } from "../order-defaults";

function ticket(overrides: Partial<TicketInput> = {}): TicketInput {
  return {
    mode: "PAPER",
    symbol: "SBIN",
    side: "BUY",
    quantity: 10,
    lots: null,
    orderType: "LIMIT",
    price: 812.5,
    trigger: 0,
    riskOverrideReason: "",
    ...overrides,
  };
}

function result(overrides: Partial<PlaceOrderResult> = {}): PlaceOrderResult {
  return {
    unconfirmed: false,
    status: "PLACED",
    message: undefined,
    orderId: "ord-1",
    duplicate: false,
    warnings: [],
    ...overrides,
  };
}

describe("inferInstrumentType", () => {
  it.each([
    ["NIFTY26OCT25000CE", "CE"],
    ["NIFTY26OCT25000PE", "PE"],
    ["SBIN", "EQ"],
    ["NSE:SBIN-EQ", "EQ"],
  ])("%s is %s", (symbol, expected) => {
    expect(inferInstrumentType(symbol)).toBe(expected);
  });
});

describe("optionLotSize", () => {
  it.each([
    ["NIFTY26OCT25000CE", 75],
    ["NSE:BANKNIFTY26OCT52000PE", 30],
    ["FINNIFTY26OCT24000CE", 25],
    ["MIDCPNIFTY26OCT12000CE", null],
    ["SBIN", null],
  ])("%s has lot size %s", (symbol, expected) => {
    expect(optionLotSize(symbol)).toBe(expected);
  });
});

describe("isStopOrder", () => {
  it.each([
    ["MARKET", false],
    ["LIMIT", false],
    ["SL", true],
    ["SL-M", true],
  ] as [OrderType, boolean][])("%s -> %s", (orderType, expected) => {
    expect(isStopOrder(orderType)).toBe(expected);
  });
});

describe("orderInputError", () => {
  it.each([
    ["a valid LIMIT order", ticket(), null],
    ["no symbol", ticket({ symbol: "" }), "Select symbol and enter valid quantity"],
    ["zero quantity", ticket({ quantity: 0 }), "Select symbol and enter valid quantity"],
    ["a LIMIT order at 0", ticket({ price: 0 }), "Enter a price greater than 0"],
    ["a MARKET order at 0", ticket({ orderType: "MARKET", price: 0 }), null],
    [
      "an SL-M order at 0 with a trigger",
      ticket({ orderType: "SL-M", price: 0, trigger: 5 }),
      null,
    ],
    [
      "an SL order without a trigger",
      ticket({ orderType: "SL", trigger: 0 }),
      "Enter a trigger price greater than 0",
    ],
    [
      "an SL-M order without a trigger",
      ticket({ orderType: "SL-M", price: 0, trigger: 0 }),
      "Enter a trigger price greater than 0",
    ],
  ])("%s", (_name, input, expected) => {
    expect(orderInputError(input)).toBe(expected);
  });
});

describe("orderDraft and orderBody", () => {
  it("builds a PAPER equity LIMIT body in wire order", () => {
    const body = orderBody(orderDraft(ticket({ riskOverrideReason: "   " })), {
      liveConfirmed: false,
      clientOrderId: "cid-1",
    });
    expect(Object.entries(body)).toEqual([
      ["mode", "PAPER"],
      ["symbol", "SBIN"],
      ["side", "BUY"],
      ["quantity", 10],
      ["product", "INTRADAY"],
      ["type", "LIMIT"],
      ["price", 812.5],
      ["trigger_price", 0],
      ["tag", "terminal-equity"],
      ["instrument_type", "EQ"],
      ["is_live_confirmation_ack", false],
      ["risk_override_reason", null],
      ["client_order_id", "cid-1"],
    ]);
  });

  it("sends a MARKET option at price 0 with its lots and a trimmed reason", () => {
    const draft = orderDraft(
      ticket({
        mode: "LIVE",
        symbol: "NIFTY26OCT25000CE",
        side: "SELL",
        quantity: 150,
        lots: { count: 2, size: 75 },
        orderType: "MARKET",
        price: 120,
        trigger: 99,
        riskOverrideReason: "  hedge  ",
      }),
    );
    expect(draft).toEqual({
      body: {
        mode: "LIVE",
        symbol: "NIFTY26OCT25000CE",
        side: "SELL",
        quantity: 150,
        product: "INTRADAY",
        type: "MARKET",
        price: 0,
        trigger_price: 0,
        tag: "terminal-options",
        instrument_type: "CE",
        risk_override_reason: "hedge",
      },
      lots: { count: 2, size: 75 },
    });
    expect(orderBody(draft, { liveConfirmed: true, clientOrderId: "cid-2" })).toMatchObject({
      is_live_confirmation_ack: true,
      client_order_id: "cid-2",
    });
  });

  it.each([
    ["SL" as const, 800, 805, 800, 805],
    ["SL-M" as const, 800, 805, 800, 805],
    ["LIMIT" as const, 800, 805, 800, 0],
  ])(
    "a %s order sends price %s->%s and trigger %s->%s",
    (orderType, price, trigger, sentPrice, sentTrigger) => {
      const { body } = orderDraft(ticket({ orderType, price, trigger }));
      expect([body.price, body.trigger_price]).toEqual([sentPrice, sentTrigger]);
    },
  );
});

describe("orderResultMessage", () => {
  const body = orderBody(orderDraft(ticket({ mode: "LIVE", side: "SELL" })), {
    liveConfirmed: true,
    clientOrderId: "cid",
  });

  it.each([
    ["a placed order", result(), "LIVE SELL PLACED (ord-1)"],
    [
      "an order with no status or id",
      result({ status: undefined, orderId: undefined }),
      "LIVE SELL submitted (ok)",
    ],
    [
      "an order with overridden warnings",
      result({ warnings: ["Position size", "Daily loss"] }),
      "LIVE SELL PLACED (ord-1) - risk warnings: Position size; Daily loss",
    ],
    ["a duplicate", result({ duplicate: true }), "LIVE SELL PLACED (ord-1) - already placed"],
    [
      "an unconfirmed order",
      result({ unconfirmed: true, message: "broker timed out" }),
      "LIVE SELL unconfirmed: broker timed out",
    ],
    [
      "an unconfirmed order without a message",
      result({ unconfirmed: true }),
      "LIVE SELL unconfirmed: check the broker orderbook",
    ],
  ])("%s", (_name, placed, expected) => {
    expect(orderResultMessage(body, placed)).toBe(expected);
  });
});
