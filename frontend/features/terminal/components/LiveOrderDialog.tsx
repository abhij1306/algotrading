import { useRef, useState } from "react";
import { Button } from "@/components/ui";
import { useRiskCheck } from "@/lib/queries/terminal";
import { liveOrderRows, riskCheckBody, riskPanelText } from "../lib/live-order";
import type { OrderDraft } from "../lib/order";

let dialogOpenings = 0;

/** A new id for each time the dialog opens. */
function nextOpenId(): number {
  dialogOpenings += 1;
  return dialogOpenings;
}

/**
 * The backend's risk check for this order, run when the dialog opens; Confirm waits for it so
 * warnings show first. Placing the order checks again, so its result never allows an order.
 */
function RiskCheckPanel({
  draft,
  check,
}: Readonly<{ draft: OrderDraft; check: ReturnType<typeof useRiskCheck> }>) {
  const hasReason = draft.body.risk_override_reason !== null;
  const text = riskPanelText(check, hasReason);
  const failed = check.data?.status === "FAIL";
  return (
    <section
      aria-label="Risk check"
      aria-live="polite"
      className={`mb-6 min-h-[2.5rem] text-xs ${failed ? "text-loss" : "text-foreground-muted"}`}
    >
      <p>{text}</p>
      {check.data && check.data.warnings.length > 0 && (
        <ul className="mt-1 list-disc pl-4 text-amber-400">
          {check.data.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      )}
    </section>
  );
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
  // One id per opening: the risk check is never an earlier opening's cached answer.
  const [openId] = useState(nextOpenId);
  const check = useRiskCheck(riskCheckBody(draft), openId);

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
      <dl id="live-order-fields" className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-sm mb-4">
        {liveOrderRows(draft).map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-foreground-muted">{label}</dt>
            <dd className="tabular-nums text-right">{value}</dd>
          </div>
        ))}
      </dl>
      <RiskCheckPanel draft={draft} check={check} />
      <div className="flex items-center justify-end gap-2">
        <Button ref={cancelRef} variant="secondary" className="h-8 text-sm" onClick={onCancel}>
          Cancel
        </Button>
        <Button
          variant={side === "BUY" ? "profit" : "loss"}
          className="h-8 text-sm"
          // The user sees the risk result (or that it is unavailable) before they can confirm.
          disabled={check.isPending}
          onClick={onConfirm}
        >
          Confirm LIVE {side}
        </Button>
      </div>
    </dialog>
  );
}
