import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vite-plus/test";
import type { PositionRow } from "@/lib/queries/strategies";
import { PositionTable } from "../PositionTable";

const UNPRICED: PositionRow = {
  id: 3,
  symbol: "TCS",
  entry_date: "2026-10-01",
  entry_price: 3890,
  shares: 10,
  stop_price: 3700,
  ltp: null,
  unrealized_pnl: null,
  unrealized_pnl_pct: null,
  r_multiple: null,
  price_status: "PRICE_UNAVAILABLE",
  two_r_status: "NOT_HIT",
  status: "OPEN",
};

function cellsOf(symbol: string): string[] {
  const row = screen.getByRole("row", { name: new RegExp(symbol) });
  return within(row)
    .getAllByRole("cell")
    .map((cell) => cell.textContent ?? "");
}

describe("PositionTable", () => {
  it("shows placeholders, not zero, when the price is unavailable", () => {
    render(<PositionTable rows={[UNPRICED]} onClose={() => {}} busy={false} />);

    const [, entry, , ltp, pnl, r, status] = cellsOf("TCS");
    expect(entry).toBe("₹3,890.00");
    expect([ltp, pnl, r]).toEqual(["--", "----", "--"]);
    expect(status).toBe("OPENPrice unavailable");
  });

  it("shows the live price and where it sits against the stop", () => {
    render(
      <PositionTable
        rows={[
          {
            ...UNPRICED,
            ltp: 3650,
            unrealized_pnl: -2400,
            unrealized_pnl_pct: -6.17,
            r_multiple: -1.26,
            price_status: "STOP_HIT",
          },
        ]}
        onClose={() => {}}
        busy={false}
      />,
    );

    const [, , , ltp, , r, status] = cellsOf("TCS");
    expect(ltp).toBe("₹3,650.00");
    expect(r).toBe("-1.26");
    expect(status).toBe("OPENStop hit");
  });
});
