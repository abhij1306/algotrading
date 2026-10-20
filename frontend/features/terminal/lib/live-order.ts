import { formatNumber, roundToDecimals } from "@/lib/format";
import type { RiskCheck, RiskCheckBody } from "@/lib/queries/terminal";
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

/** The risk-check fields of a draft: what the order would be checked as when placed. */
export function riskCheckBody(draft: OrderDraft): RiskCheckBody {
  const { body } = draft;
  return {
    symbol: body.symbol,
    side: body.side,
    quantity: body.quantity,
    product: body.product,
    type: body.type,
    price: body.price,
    instrument_type: body.instrument_type,
  };
}

function riskCheckText(check: RiskCheck, hasReason: boolean): string {
  if (check.status === "FAIL") return `Will be rejected: ${check.message}`;
  if (check.status === "PASS") return "All risk checks passed.";
  return hasReason
    ? "Risk warnings, overridden by your reason:"
    : "Risk warnings: add a risk override reason, or the order will be rejected.";
}

/** The risk panel's line for the check's state: its answer, else unavailable, else pending. */
export function riskPanelText(
  check: { isError: boolean; data: RiskCheck | undefined },
  hasReason: boolean,
): string {
  if (check.data) return riskCheckText(check.data, hasReason);
  if (check.isError) return "Risk check unavailable; the order is checked again when placed.";
  return "Checking risk...";
}
