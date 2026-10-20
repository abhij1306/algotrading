import { Button, Input } from "@/components/ui";
import { useOrderTicket, type OrderTicketState } from "../hooks/use-order-ticket";
import { isStopOrder } from "../lib/order";
import type { OrderType, TradingMode } from "../lib/order-defaults";
import { atLeastOne, zeroIfBlank, type TerminalView } from "../lib/terminal-view";
import { LiveOrderDialog } from "./LiveOrderDialog";

function NumberField({
  id,
  label,
  placeholder,
  value,
  min,
  disabled,
  onChange,
}: Readonly<{
  id: string;
  label: string;
  placeholder: string;
  value: number;
  min?: number;
  disabled?: boolean;
  onChange: (raw: string) => void;
}>) {
  return (
    <div>
      <label htmlFor={id} className="text-xs text-foreground-muted mb-1 block">
        {label}
      </label>
      <Input
        id={id}
        type="number"
        min={min}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        className="h-8 text-sm"
      />
    </div>
  );
}

function OrderFields({
  ticket,
  tradingMode,
}: Readonly<{ ticket: OrderTicketState; tradingMode: TradingMode }>) {
  return (
    <>
      <div>
        <label htmlFor="order-type" className="text-xs text-foreground-muted mb-1 block">
          Order Type
        </label>
        <select
          id="order-type"
          value={ticket.orderType}
          onChange={(event) => ticket.setOrderType(event.target.value as OrderType)}
          className="w-full h-8 px-2 rounded text-sm border border-border bg-background outline-none"
        >
          <option value="MARKET">Market</option>
          <option value="LIMIT">Limit</option>
          <option value="SL">Stop Loss</option>
          <option value="SL-M">Stop Loss Market</option>
        </select>
      </div>
      {ticket.lotsMode ? (
        <NumberField
          id="order-lots"
          label="Lots"
          placeholder="Lots"
          min={1}
          value={ticket.optionLots}
          onChange={(raw) => ticket.setOptionLots(atLeastOne(raw))}
        />
      ) : (
        <NumberField
          id="order-qty"
          label="Quantity"
          placeholder="Qty"
          min={1}
          value={ticket.orderQty}
          onChange={(raw) => ticket.setOrderQty(atLeastOne(raw))}
        />
      )}
      <NumberField
        id="order-price"
        label="Price"
        placeholder="Price"
        value={ticket.orderPrice}
        disabled={ticket.orderType === "MARKET"}
        onChange={(raw) => ticket.setOrderPrice(zeroIfBlank(raw))}
      />
      {isStopOrder(ticket.orderType) && (
        <NumberField
          id="order-trigger"
          label="Trigger"
          placeholder="Trigger"
          value={ticket.orderTrigger}
          onChange={(raw) => ticket.setOrderTrigger(zeroIfBlank(raw))}
        />
      )}
      {tradingMode === "LIVE" && (
        <div>
          <label
            htmlFor="risk-override-reason"
            className="text-xs text-foreground-muted mb-1 block"
          >
            Risk Override Reason (if warning)
          </label>
          <Input
            id="risk-override-reason"
            type="text"
            value={ticket.riskOverrideReason}
            onChange={(event) => ticket.setRiskOverrideReason(event.target.value)}
            placeholder="Optional unless risk warning"
            className="h-8 text-sm"
          />
        </div>
      )}
    </>
  );
}

export function OrderTicket({
  tradingMode,
  terminalView,
  activeTradeSymbol,
  message: orderMessage,
  onMessage,
}: Readonly<{
  tradingMode: TradingMode;
  terminalView: TerminalView;
  activeTradeSymbol: string;
  message: string | null;
  onMessage: (message: string | null) => void;
}>) {
  const ticket = useOrderTicket({ tradingMode, terminalView, activeTradeSymbol, onMessage });

  return (
    <div className="w-56 flex-shrink-0 p-3 border-l border-border bg-surface">
      <div className="mb-2 text-xs text-foreground-muted">
        Trade Symbol:{" "}
        <span className="tabular-nums text-foreground">{activeTradeSymbol || "--"}</span>
      </div>
      {ticket.lotsMode && (
        <div className="mb-2 text-xs text-foreground-muted">
          Lot Size: <span className="tabular-nums text-foreground">{ticket.lotSize ?? "--"}</span> |
          Lots: <span className="tabular-nums text-foreground">{ticket.optionLots}</span> | Qty:{" "}
          <span className="tabular-nums text-foreground">{ticket.quantity ?? "--"}</span>
        </div>
      )}
      <div className="flex items-center gap-2 mb-3">
        <Button
          variant="profit"
          className="flex-1 h-8 text-sm"
          disabled={ticket.isSubmitting}
          onClick={() => ticket.placeOrder("BUY")}
        >
          Buy
        </Button>
        <Button
          variant="loss"
          className="flex-1 h-8 text-sm"
          disabled={ticket.isSubmitting}
          onClick={() => ticket.placeOrder("SELL")}
        >
          Sell
        </Button>
      </div>

      <div className="space-y-3">
        <OrderFields ticket={ticket} tradingMode={tradingMode} />
        {orderMessage && (
          <div className="text-xs text-foreground-muted border border-border rounded px-2 py-1.5">
            {orderMessage}
          </div>
        )}
      </div>
      {ticket.pendingLive && (
        <LiveOrderDialog
          draft={ticket.pendingLive}
          onConfirm={ticket.confirmLive}
          onCancel={ticket.cancelLive}
        />
      )}
    </div>
  );
}
