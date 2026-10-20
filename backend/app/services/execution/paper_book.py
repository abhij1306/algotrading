"""Exact money math for PAPER trading: fill pricing, charges and the paper position book.

All arithmetic is `Decimal`. Values enter through `to_decimal` (a float goes through its
shortest repr, so 0.1 is exactly 0.1, not 0.1000000000000000055...) and money leaves
through `to_money`, which quantizes to paise (0.01) with ROUND_HALF_UP, the rounding a
human expects (100.005 -> 100.01). Quantization happens only at boundaries:

- a simulated fill price and its estimated charges are quantized when produced, because
  they are stored on the order row;
- a position keeps its exact cost basis (unquantized) so realised P&L on later
  reductions does not drift; its average entry price is quantized only when serialised;
- realised, unrealised and net P&L are quantized when serialised.

The live_orders price columns are Float, so callers convert a quantized Decimal to float
when storing it and when building a JSON response. Every quantized value has at most two
decimal places, so the float round-trip is the nearest float to that paise amount.

This module imports no broker code: nothing here can reach the live broker.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

PAISE = Decimal("0.01")
ZERO = Decimal("0")
BPS_DIVISOR = Decimal("10000")


def to_decimal(value: object, default: Decimal = ZERO) -> Decimal:
    """`value` as a Decimal, or `default` when it is missing or not a finite number."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, Decimal):
        result = value
    else:
        try:
            # str() first: Decimal(0.1) is the binary float's exact expansion.
            result = Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            return default
    return result if result.is_finite() else default


def to_money(value: Decimal) -> Decimal:
    """Quantize to paise, rounding half up."""
    return value.quantize(PAISE, rounding=ROUND_HALF_UP)


def money_float(value: Decimal) -> float:
    """A money amount for a Float column or a JSON response: quantized, then float."""
    return float(to_money(value))


# --- Fill pricing ------------------------------------------------------------------


def slipped_price(side: str, ref: Decimal, slippage_bps: Decimal) -> Decimal:
    """Reference price moved against the trader by `slippage_bps`, never below zero."""
    slip = ref * slippage_bps / BPS_DIVISOR
    return ref + slip if side == "BUY" else max(ZERO, ref - slip)


def fill_price(side: str, ref: Decimal, slippage_bps: Decimal, limit: Decimal | None) -> Decimal:
    """Quantized fill price. A limit caps the slipped price (never fills past the limit)."""
    price = slipped_price(side, ref, slippage_bps)
    if limit is not None:
        price = min(price, limit) if side == "BUY" else max(price, limit)
    return to_money(price)


def estimated_charges(price: Decimal, quantity: int, fee_bps: Decimal) -> Decimal:
    """Quantized charges on a fill's notional (price x quantity)."""
    if quantity <= 0:
        return to_money(ZERO)
    return to_money(price * quantity * fee_bps / BPS_DIVISOR)


# --- Position book -----------------------------------------------------------------


@dataclass
class PaperPosition:
    """Net position in one symbol, built by applying fills in time order.

    The book keeps `cost`, the exact cost basis of the open quantity (sum of price x
    quantity, average-cost method), rather than a stored average: an average such as
    302/3 has no finite decimal form, while the cost basis does. Closing a position in
    full therefore removes exactly the cost that was paid. `realized_pnl` accumulates on
    every reduction.
    """

    net_qty: int = 0
    cost: Decimal = ZERO
    realized_pnl: Decimal = ZERO

    @property
    def entry_price(self) -> Decimal:
        """Average price of the open quantity, 0 when flat (unquantized)."""
        return self.cost / abs(self.net_qty) if self.net_qty else ZERO

    def apply_fill(self, side: str, quantity: int, price: Decimal) -> None:
        """Apply a BUY or SELL fill of `quantity` at `price`.

        Adding to a position adds to the cost basis (re-averaging the entry); reducing it
        realises P&L on the closed quantity against its share of the cost basis; crossing
        zero closes the old side in full and opens the remainder on the other side at
        `price`.
        """
        side = side.upper()
        if quantity <= 0 or side not in {"BUY", "SELL"}:
            raise ValueError(f"Invalid paper fill: {side} {quantity}")
        delta = quantity if side == "BUY" else -quantity
        net = self.net_qty

        if net == 0 or (net > 0) == (delta > 0):
            self.cost += price * quantity
            self.net_qty = net + delta
            return

        closing = min(abs(net), quantity)
        # Full close removes the whole basis, so no division (and no rounding) happens.
        closed_cost = self.cost if closing == abs(net) else self.cost * closing / abs(net)
        proceeds = price * closing
        self.realized_pnl += proceeds - closed_cost if net > 0 else closed_cost - proceeds
        self.cost -= closed_cost
        self.net_qty = net + delta
        opened = quantity - closing
        if opened:
            # Reversal: the remainder opens a new position on the other side at `price`.
            self.cost = price * opened

    def unrealized_pnl(self, mark: Decimal) -> Decimal:
        """P&L of the open quantity marked at `mark` (exact; unquantized)."""
        if self.net_qty > 0:
            return mark * self.net_qty - self.cost
        return self.cost - mark * abs(self.net_qty)
