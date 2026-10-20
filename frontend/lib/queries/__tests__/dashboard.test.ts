import { describe, expect, it } from "vite-plus/test";
import { formatIstClock, mergeCommodities, type CommodityData } from "../dashboard";

const row = (name: string, price: number): CommodityData => ({
  name,
  symbol: name,
  price,
  changePercent: 0.1,
});

describe("mergeCommodities", () => {
  it("keeps the overview rows and appends only the names the overview lacks", () => {
    const merged = mergeCommodities(
      [row("Gold", 2400), row("Silver", 30)],
      [row("Gold", 2399), row("Crude Oil", 78.4)],
    );
    expect(merged.map((item) => [item.name, item.price])).toEqual([
      ["Gold", 2400],
      ["Silver", 30],
      ["Crude Oil", 78.4],
    ]);
  });

  it("returns the overview rows when there are no extras", () => {
    expect(mergeCommodities([row("Gold", 2400)], [])).toEqual([row("Gold", 2400)]);
  });
});

describe("formatIstClock", () => {
  it.each([
    ["2026-10-20T03:45:00Z", "09:15:00 IST"],
    ["2026-10-16T18:30:00Z", "00:00:00 IST"],
    ["2026-10-16T18:29:59Z", "23:59:59 IST"],
  ])("%s is %s", (iso, expected) => {
    expect(formatIstClock(new Date(iso))).toBe(expected);
  });
});
