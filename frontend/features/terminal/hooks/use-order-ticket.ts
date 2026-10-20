import { useState } from "react";
import { usePlaceOrder } from "@/lib/queries/terminal";
import { DEFAULT_ORDER_TYPE, type OrderType, type TradingMode } from "../lib/order-defaults";
import {
  orderBody,
  orderDraft,
  orderInputError,
  orderResultMessage,
  type OrderDraft,
  type OrderSide,
} from "../lib/order";
import { errorText, ticketSizing, type TerminalView } from "../lib/terminal-view";

/**
 * Order ticket inputs and submission. Options are sized in lots of the underlying.
 * PAPER sends on click. LIVE snapshots the order into `pendingLive`, the dialog shows it, and
 * only its Confirm sends that snapshot with the acknowledgement; Cancel sends nothing.
 */
export function useOrderTicket(params: {
  tradingMode: TradingMode;
  terminalView: TerminalView;
  activeTradeSymbol: string;
  onMessage: (message: string | null) => void;
}) {
  const { tradingMode, terminalView, activeTradeSymbol, onMessage } = params;
  const [orderType, setOrderType] = useState<OrderType>(DEFAULT_ORDER_TYPE);
  const [orderQty, setOrderQty] = useState<number>(1);
  const [optionLots, setOptionLots] = useState<number>(1);
  const [orderPrice, setOrderPrice] = useState<number>(0);
  const [orderTrigger, setOrderTrigger] = useState<number>(0);
  const [riskOverrideReason, setRiskOverrideReason] = useState<string>("");
  const [pendingLive, setPendingLive] = useState<OrderDraft | null>(null);
  const submit = usePlaceOrder();

  const { lotsMode, lotSize, lots, quantity } = ticketSizing({
    view: terminalView,
    symbol: activeTradeSymbol,
    orderQty,
    optionLots,
  });

  const send = (draft: OrderDraft, liveConfirmed: boolean) => {
    // A new click is a new intent; retries of this mutation reuse this id.
    const body = orderBody(draft, { liveConfirmed, clientOrderId: crypto.randomUUID() });
    submit.mutate(body, {
      onSuccess: (result) => onMessage(orderResultMessage(body, result)),
      onError: (error) => onMessage(errorText(error, "Order failed")),
    });
  };

  const placeOrder = (side: OrderSide) => {
    if (quantity === null) {
      onMessage(`Unknown lot size for ${activeTradeSymbol}`);
      return;
    }
    const input = {
      mode: tradingMode,
      symbol: activeTradeSymbol,
      side,
      quantity,
      lots,
      orderType,
      price: orderPrice,
      trigger: orderTrigger,
      riskOverrideReason,
    };
    const invalid = orderInputError(input);
    if (invalid) {
      onMessage(invalid);
      return;
    }
    onMessage(null);
    const draft = orderDraft(input);
    if (tradingMode === "LIVE") {
      setPendingLive(draft);
      return;
    }
    send(draft, false);
  };

  const confirmLive = () => {
    if (!pendingLive) return;
    setPendingLive(null);
    send(pendingLive, true);
  };

  const cancelLive = () => {
    setPendingLive(null);
    onMessage("Live order cancelled");
  };

  return {
    orderType,
    setOrderType,
    orderQty,
    setOrderQty,
    optionLots,
    setOptionLots,
    orderPrice,
    setOrderPrice,
    orderTrigger,
    setOrderTrigger,
    riskOverrideReason,
    setRiskOverrideReason,
    lotsMode,
    lotSize,
    quantity,
    isSubmitting: submit.isPending,
    placeOrder,
    pendingLive,
    confirmLive,
    cancelLive,
  };
}

export type OrderTicketState = ReturnType<typeof useOrderTicket>;
