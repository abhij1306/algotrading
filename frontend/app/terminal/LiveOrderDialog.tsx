import { useRef } from "react";
import { Button } from "@/components/ui";
import { formatNumber, roundToDecimals } from "@/lib/format";
import type { OrderDraft } from "./order";

const NONE = "None";

/** A price exactly as it will be sent: two decimals unless the value has more, never rounded. */
function exactPrice(value: number): string {
  return roundToDecimals(value, 2) === value ? formatNumber(value) : String(value);
}

/** Every field the LIVE order sends, labelled for a person, in the order the ticket shows them. */
export function liveOrderRows(draft: OrderDraft): [label: string, value: string][] {
  const { body, lots } = draft;
  const quantity = lots
    ? `${body.quantity} (${lots.count} lots of ${lots.size})`
    : String(body.quantity);
  return [
    ["Mode", body.mode],
    ["Side", body.side],
    ["Symbol", body.symbol],
    ["Instrument", body.instrument_type ?? NONE],
    ["Quantity", quantity],
    ["Order type", body.type],
    ["Price", body.type === "MARKET" ? "Market" : exactPrice(body.price)],
    ["Trigger", body.trigger_price ? exactPrice(body.trigger_price) : NONE],
    ["Product", body.product],
    ["Risk override reason", body.risk_override_reason ?? NONE],
  ];
}

/**
 * Modal confirmation for a LIVE order. Focus starts on Cancel, so Enter never places the order;
 * Escape cancels. Mount it only while an order awaits confirmation.
 */
export function LiveOrderDialog({
  draft,
  onConfirm,
  onCancel,
}: Readonly<{ draft: OrderDraft; onConfirm: () => void; onCancel: () => void }>) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  // Child refs attach first, so Cancel exists by the time the dialog opens.
  const openModal = (dialog: HTMLDialogElement | null) => {
    if (!dialog || dialog.open) return;
    dialog.showModal();
    cancelRef.current?.focus();
  };
  const { side, symbol } = draft.body;

  return (
    <dialog
      ref={openModal}
      aria-labelledby="live-order-title"
      aria-describedby="live-order-fields"
      onCancel={(event) => {
        event.preventDefault();
        onCancel();
      }}
      className="bg-base text-foreground border border-border-subtle rounded-xl p-6 w-[420px] max-w-[calc(100vw-2rem)] shadow-2xl backdrop:bg-void/80 backdrop:backdrop-blur-sm"
    >
      <h3 id="live-order-title" className="text-lg font-semibold text-loss mb-4">
        Place LIVE {side} order for {symbol}?
      </h3>
      <dl id="live-order-fields" className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-sm mb-6">
        {liveOrderRows(draft).map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-foreground-muted">{label}</dt>
            <dd className="tabular-nums text-right">{value}</dd>
          </div>
        ))}
      </dl>
      <div className="flex items-center justify-end gap-2">
        <Button ref={cancelRef} variant="secondary" className="h-8 text-sm" onClick={onCancel}>
          Cancel
        </Button>
        <Button
          variant={side === "BUY" ? "profit" : "loss"}
          className="h-8 text-sm"
          onClick={onConfirm}
        >
          Confirm LIVE {side}
        </Button>
      </div>
    </dialog>
  );
}
