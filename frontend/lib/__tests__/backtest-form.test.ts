import { describe, expect, it } from "vite-plus/test";
import {
  addableRules,
  newDraft,
  startBody,
  variantBody,
  versionFields,
  type Draft,
  type VersionOption,
} from "../backtest-form";

const VERSION: VersionOption = {
  id: "v1",
  strategy: "stage2-contraction-breakout",
  version: 1,
  status: "draft",
  exits: [
    { rule: "hard_stop", enabled: true },
    { rule: "move_stop_to_breakeven", at_r: 2, after: null, enabled: true },
    { rule: "partial_at_r", at_r: 3, sell_fraction: 0.33, enabled: true },
    { rule: "close_below_sma", sma: 50, sell_fraction: 1, enabled: true },
    { rule: "time_stop", days: 15, min_r: 1, enabled: false },
  ],
  playbooks: [
    {
      name: "default",
      stop: { rule: "below_final_contraction_low", max_pct: 8, min_pct: 0, buffer_pct: 0.5 },
    },
  ],
  universes: [{ id: "u1", name: "nifty500" }],
};
const FIELDS = versionFields(VERSION);

function draft(change: Partial<Draft> = {}): Draft {
  return { ...newDraft("base"), ...change };
}

describe("versionFields", () => {
  it("lists the stop's settings, then each exit's switch and numbers by index", () => {
    const paths = FIELDS.map((field) => field.path);
    expect(paths.slice(0, 3)).toEqual([
      "playbooks[0].stop.max_pct",
      "playbooks[0].stop.min_pct",
      "playbooks[0].stop.buffer_pct",
    ]);
    expect(paths).not.toContain("exits[0].enabled");
    expect(paths).toContain("exits[3].start_r");
    expect(FIELDS.find((f) => f.path === "exits[4].enabled")?.initial).toBe("false");
    expect(FIELDS.find((f) => f.path === "exits[3].start_r")?.initial).toBe("");
  });

  it("offers only the addable rules the stack lacks", () => {
    expect(addableRules(VERSION)).toEqual([
      "max_hold",
      "early_failure",
      "trailing_atr",
      "step_trail",
      "close_below_ema",
    ]);
  });
});

describe("optional settings", () => {
  const trail: VersionOption = {
    ...VERSION,
    exits: [
      { rule: "move_stop_to_breakeven", at_r: null, after: "partial", enabled: true },
      {
        rule: "trailing_swing_low",
        lookback_days: 10,
        sell_fraction: 1,
        start_r: 2,
        enabled: true,
      },
    ],
    playbooks: [{ name: "default", stop: { rule: "swing_low", lookback_days: 10, max_pct: 12 } }],
  };
  const fields = versionFields(trail);

  it("offers no at_r for a breakeven triggered by a partial", () => {
    expect(fields.map((f) => f.path)).not.toContain("exits[0].at_r");
  });

  it("turns an optional setting off when cleared, and keeps a required one", () => {
    const body = variantBody(
      fields,
      draft({
        values: {
          "exits[1].start_r": "",
          "playbooks[0].stop.max_pct": " ",
          "exits[1].lookback_days": "",
        },
      }),
    );
    expect(body).toEqual({
      ok: true,
      value: {
        label: "base",
        sets: [
          { path: "playbooks[0].stop.max_pct", value: null },
          { path: "exits[1].start_r", value: null },
        ],
      },
    });
  });
});

describe("variantBody", () => {
  it("sends only what differs from the version, then the added rules", () => {
    const body = variantBody(
      FIELDS,
      draft({
        values: {
          "exits[2].at_r": "4",
          "exits[3].sma": "50.0", // unchanged
          "exits[3].start_r": "", // left unset
          "exits[4].enabled": "true",
          "playbooks[0].stop.max_pct": "12",
          "+max_hold.days": "90",
        },
        added: ["max_hold"],
        riskPct: "0.005",
        maxPositions: "8",
        sleeveWeight: "0.5",
        slotOrder: "random",
        seed: "3",
      }),
    );
    expect(body).toEqual({
      ok: true,
      value: {
        label: "base",
        sets: [
          { path: "playbooks[0].stop.max_pct", value: 12 },
          { path: "exits[2].at_r", value: 4 },
          { path: "exits[4].enabled", value: true },
          { path: "exits[+]", value: { rule: "max_hold", days: 90 } },
        ],
        risk_pct: "0.005",
        max_positions: 8,
        sleeve_weight: "0.5",
        slot_order: "random:3",
      },
    });
  });

  it("refuses a fraction where a whole number goes, and a malformed decimal", () => {
    expect(variantBody(FIELDS, draft({ values: { "exits[3].sma": "20.5" } }))).toEqual({
      ok: false,
      error: "close_below_sma sma: a whole number",
    });
    expect(variantBody(FIELDS, draft({ riskPct: "1e-3" }))).toEqual({
      ok: false,
      error: "Risk per trade: a decimal, e.g. 0.5",
    });
  });

  it("sends a plain run with no overrides as the version is", () => {
    expect(variantBody(FIELDS, draft())).toEqual({ ok: true, value: { label: "base", sets: [] } });
  });
});

describe("startBody", () => {
  const form = { versionId: "v1", universeId: "u1", start: "2019-01-01", end: "2024-12-31" };

  it("refuses an empty window and a sweep with a repeated label", () => {
    expect(startBody(VERSION, { ...form, end: "2019-01-01", drafts: [draft()] }).ok).toBe(false);
    expect(startBody(VERSION, { ...form, drafts: [draft(), draft()] })).toEqual({
      ok: false,
      error: "Each variant needs its own label",
    });
  });

  it("builds a sweep of the variants on one version, universe and window", () => {
    const body = startBody(VERSION, {
      ...form,
      drafts: [draft(), draft({ label: "risk 1%", riskPct: "0.01" })],
    });
    expect(body).toEqual({
      ok: true,
      value: {
        strategy_version_id: "v1",
        universe_id: "u1",
        start: "2019-01-01",
        end: "2024-12-31",
        variants: [
          { label: "base", sets: [] },
          { label: "risk 1%", sets: [], risk_pct: "0.01" },
        ],
      },
    });
  });
});
