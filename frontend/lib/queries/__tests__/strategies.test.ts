import { describe, expect, it } from "vite-plus/test";
import { positionRowSchema } from "../strategies";

const ROW = {
  id: 3,
  symbol: "TCS",
  entry_date: "2026-10-01",
  entry_price: 3890,
  shares: 10,
  stop_price: 3700,
  two_r_status: "NOT_HIT",
  status: "OPEN",
};

describe("positionRowSchema", () => {
  it("keeps a priced row as sent", () => {
    const parsed = positionRowSchema.parse({
      ...ROW,
      ltp: 3950,
      unrealized_pnl: 600,
      unrealized_pnl_pct: 1.54,
      r_multiple: 0.32,
      price_status: "TARGET_HIT",
    });

    expect([
      parsed.ltp,
      parsed.unrealized_pnl,
      parsed.unrealized_pnl_pct,
      parsed.r_multiple,
      parsed.price_status,
    ]).toEqual([3950, 600, 1.54, 0.32, "TARGET_HIT"]);
  });

  it("passes a missing quote through as null, not 0", () => {
    const parsed = positionRowSchema.parse({
      ...ROW,
      ltp: null,
      unrealized_pnl: null,
      unrealized_pnl_pct: null,
      r_multiple: null,
      price_status: "PRICE_UNAVAILABLE",
    });

    expect([
      parsed.ltp,
      parsed.unrealized_pnl,
      parsed.unrealized_pnl_pct,
      parsed.r_multiple,
      parsed.price_status,
    ]).toEqual([null, null, null, null, "PRICE_UNAVAILABLE"]);
  });

  it("reads garbage prices as null and an unknown status as unavailable", () => {
    const parsed = positionRowSchema.parse({
      ...ROW,
      ltp: "3950",
      unrealized_pnl: {},
      unrealized_pnl_pct: true,
      r_multiple: "n/a",
      price_status: "HALTED",
    });

    expect([
      parsed.ltp,
      parsed.unrealized_pnl,
      parsed.unrealized_pnl_pct,
      parsed.r_multiple,
      parsed.price_status,
    ]).toEqual([null, null, null, null, "PRICE_UNAVAILABLE"]);
  });
});
