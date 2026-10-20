import type { PlaceOrderBody, PlaceOrderResult } from "@/lib/queries/terminal";
import type { OrderType, TradingMode } from "./order-defaults";

export type OrderSide = "BUY" | "SELL";
export type InstrumentType = "PE" | "CE" | "EQ";

export function inferInstrumentType(symbol: string): InstrumentType {
  if (symbol.endsWith("PE")) return "PE";
  if (symbol.endsWith("CE")) return "CE";
  return "EQ";
}

/** The order tag names the flow: the instrument decides, not the terminal view. */
function orderTag(symbol: string): "terminal-equity" | "terminal-options" {
  return inferInstrumentType(symbol) === "EQ" ? "terminal-equity" : "terminal-options";
}

export function isStopOrder(orderType: OrderType): boolean {
  return orderType === "SL" || orderType === "SL-M";
}

const isPositive = (value: number): boolean => value > 0;

/** The order ticket's inputs at the moment Buy or Sell is clicked. */
export interface TicketInput {
  mode: TradingMode;
  symbol: string;
  side: OrderSide;
  quantity: number;
  /** Set when the ticket sizes an option in lots of the underlying. */
  lots: { count: number; size: number } | null;
  orderType: OrderType;
  price: number;
  trigger: number;
  riskOverrideReason: string;
}

/** A user-facing message when the order inputs are unusable, else null. */
export function orderInputError(input: TicketInput): string | null {
  const { symbol, quantity, orderType, price, trigger } = input;
  if (!symbol || quantity <= 0) return "Select symbol and enter valid quantity";
  if (orderType !== "MARKET" && orderType !== "SL-M" && !isPositive(price)) {
    return "Enter a price greater than 0";
  }
  if (isStopOrder(orderType) && !isPositive(trigger)) {
    return "Enter a trigger price greater than 0";
  }
  return null;
}

type DraftBody = Omit<PlaceOrderBody, "is_live_confirmation_ack" | "client_order_id"> & {
  mode: TradingMode;
  side: OrderSide;
  type: OrderType;
  price: number;
  trigger_price: number;
  instrument_type: InstrumentType;
};

/**
 * A snapshot of one order, taken when Buy or Sell is clicked. The LIVE dialog shows it and the
 * request is built from it, so later edits to the ticket cannot change what was confirmed.
 */
export interface OrderDraft {
  body: DraftBody;
  lots: TicketInput["lots"];
}

export function orderDraft(input: TicketInput): OrderDraft {
  const { orderType } = input;
  return {
    body: {
      mode: input.mode,
      symbol: input.symbol,
      side: input.side,
      quantity: input.quantity,
      product: "INTRADAY",
      type: orderType,
      price: orderType === "MARKET" ? 0 : input.price,
      trigger_price: isStopOrder(orderType) ? input.trigger : 0,
      tag: orderTag(input.symbol),
      instrument_type: inferInstrumentType(input.symbol),
      risk_override_reason: input.riskOverrideReason.trim() || null,
    },
    lots: input.lots,
  };
}

/** The request body for a draft; `liveConfirmed` is true only after the LIVE dialog's Confirm. */
export function orderBody(
  draft: OrderDraft,
  ack: { liveConfirmed: boolean; clientOrderId: string },
): PlaceOrderBody {
  const { body } = draft;
  // Key order is the wire order the backend and the payload tests have always seen.
  return {
    mode: body.mode,
    symbol: body.symbol,
    side: body.side,
    quantity: body.quantity,
    product: body.product,
    type: body.type,
    price: body.price,
    trigger_price: body.trigger_price,
    tag: body.tag,
    instrument_type: body.instrument_type,
    is_live_confirmation_ack: ack.liveConfirmed,
    risk_override_reason: body.risk_override_reason,
    client_order_id: ack.clientOrderId,
  };
}

/** The ticket message for a placed order. */
export function orderResultMessage(body: PlaceOrderBody, result: PlaceOrderResult): string {
  const { mode, side } = body;
  if (result.unconfirmed) {
    // Not rejected: the order may be live, so the user must not place it again.
    return `${mode} ${side} unconfirmed: ${result.message ?? "check the broker orderbook"}`;
  }
  const summary = `${mode} ${side} ${result.status ?? "submitted"} (${result.orderId ?? "ok"})`;
  return result.duplicate ? `${summary} - already placed` : summary;
}
