/**
 * The start form's pure parts: which settings of a strategy version it shows, and how a variant's
 * edited fields become the `POST /api/hat/backtests` body. A changed exit or stop setting is
 * sent as a config override (`path` and JSON value, app/hat/backtest/overrides.py): an exit by
 * its index in the stack (`exits[2].at_r`), a stop by its playbook's index
 * (`playbooks[0].stop.max_pct`), an added rule appended (`exits[+]`). Only what differs from
 * the version is sent.
 */
import type { components } from "@/lib/api/schema";

type Schemas = components["schemas"];
export type VersionOption = Schemas["BacktestVersionOption"];
export type StartBody = Schemas["BacktestStart"];
export type VariantBody = Schemas["BacktestVariantStart"];

type Config = Readonly<Record<string, unknown>>;

/** The editable numbers of each exit rule (every rule but `hard_stop` can also be switched). */
export const EXIT_FIELDS: Readonly<Record<string, readonly string[]>> = {
  move_stop_to_breakeven: ["at_r"],
  step_trail: ["step_r", "move_r"],
  partial_at_r: ["at_r", "sell_fraction"],
  partial_after_days: ["days", "sell_fraction"],
  close_below_sma: ["sma", "sell_fraction", "start_r"],
  close_above_sma: ["sma", "sell_fraction"],
  close_below_ema: ["ema", "sell_fraction", "start_r"],
  trailing_swing_low: ["lookback_days", "sell_fraction", "start_r"],
  trailing_atr: ["days", "multiple", "sell_fraction", "start_r"],
  pct_gain_target: ["pct", "sell_fraction"],
  time_stop: ["days", "min_r"],
  early_failure: ["days", "min_r"],
  max_hold: ["days"],
  regime_exit: ["sell_fraction"],
};

/** The editable numbers of each stop rule. */
export const STOP_FIELDS: Readonly<Record<string, readonly string[]>> = {
  below_final_contraction_low: ["max_pct", "min_pct", "buffer_pct"],
  low_of_day: ["buffer_ticks", "max_pct"],
  pct_below_entry: ["pct"],
  atr_multiple: ["days", "multiple", "max_pct"],
  swing_low: ["lookback_days", "max_pct"],
};

/** The exit rules the form can add to a stack that lacks them, with their starting values. */
export const ADDABLE: Readonly<Record<string, Readonly<Record<string, number>>>> = {
  max_hold: { days: 60 },
  early_failure: { days: 10, min_r: 0.5 },
  trailing_atr: { days: 14, multiple: 3 },
  step_trail: { step_r: 1.5, move_r: 1 },
  close_below_ema: { ema: 21 },
};

const WHOLE = new Set(["days", "sma", "ema", "lookback_days", "buffer_ticks"]);
// The optional settings: a trail's start, and the stop-width cap of the rules without their own.
const NULLABLE_STOP_CAP = new Set(["low_of_day", "atr_multiple", "swing_low"]);
const nullable = (rule: string, name: string) =>
  name === "start_r" || (name === "max_pct" && NULLABLE_STOP_CAP.has(rule));

export type Field = Readonly<{
  path: string; // the override path
  group: string; // the rule it belongs to, as shown
  name: string; // the parameter
  kind: "number" | "switch";
  whole: boolean;
  nullable: boolean; // left empty, the setting is turned off (sent as null)
  initial: string; // the version's value as typed ("" when unset)
}>;

function initialText(value: unknown): string {
  if (typeof value === "boolean") return value ? "true" : "false";
  return typeof value === "number" ? String(value) : "";
}

function ruleFields(
  prefix: string,
  group: string,
  rule: Config,
  names: readonly string[],
): Field[] {
  const kind = asText(rule["rule"] ?? "");
  return names.map((name) => ({
    path: `${prefix}.${name}`,
    group,
    name,
    kind: "number",
    whole: WHOLE.has(name),
    nullable: nullable(kind, name),
    initial: initialText(rule[name]),
  }));
}

/** The settings the form shows for `version`: each playbook's stop, then each exit in order. */
export function versionFields(version: VersionOption): Field[] {
  const fields: Field[] = [];
  version.playbooks.forEach((playbook, index) => {
    const rule = asText(playbook.stop["rule"] ?? "");
    const names = STOP_FIELDS[rule] ?? [];
    fields.push(
      ...ruleFields(
        `playbooks[${index}].stop`,
        `stop: ${playbook.name} (${rule})`,
        playbook.stop,
        names,
      ),
    );
  });
  version.exits.forEach((exit, index) => {
    const rule = asText(exit["rule"] ?? "");
    const prefix = `exits[${index}]`;
    if (rule !== "hard_stop" && "enabled" in exit) {
      fields.push({
        path: `${prefix}.enabled`,
        group: rule,
        name: "enabled",
        kind: "switch",
        whole: false,
        nullable: false,
        initial: initialText(exit["enabled"]),
      });
    }
    // A breakeven triggered by a partial (`after`) has no `at_r` to edit: setting one is refused.
    const breakevenAfter = rule === "move_stop_to_breakeven" && exit["at_r"] == null;
    const names = (EXIT_FIELDS[rule] ?? []).filter((name) => !(breakevenAfter && name === "at_r"));
    fields.push(...ruleFields(prefix, rule, exit, names));
  });
  return fields;
}

/** The rules of `ADDABLE` that `version`'s exit stack lacks. */
export function addableRules(version: VersionOption): string[] {
  const present = new Set(version.exits.map((exit) => exit["rule"]));
  return Object.keys(ADDABLE).filter((rule) => !present.has(rule));
}

/** One variant as typed. `values` holds an edited field's text by path; an added rule's
 * parameters are keyed `+<rule>.<name>`. */
export type Draft = {
  id: string; // stable across edits, for the variant tabs
  label: string;
  sleeveWeight: string;
  riskPct: string;
  maxPositions: string;
  slotOrder: string; // "time", "score" or "random"
  seed: string; // K of random:K
  values: Record<string, string>;
  added: string[];
};

export function newDraft(label: string): Draft {
  return {
    id: crypto.randomUUID(),
    label,
    sleeveWeight: "",
    riskPct: "",
    maxPositions: "",
    slotOrder: "time",
    seed: "1",
    values: {},
    added: [],
  };
}

/** A copy of `draft` under a new label, for the next variant of a sweep. */
export function copyDraft(draft: Draft, label: string): Draft {
  return {
    ...draft,
    id: crypto.randomUUID(),
    label,
    values: { ...draft.values },
    added: [...draft.added],
  };
}

type Parsed<T> = { ok: true; value: T } | { ok: false; error: string };

function parseNumber(text: string, whole: boolean, what: string): Parsed<number> {
  const value = Number(text.trim());
  if (text.trim() === "" || !Number.isFinite(value) || (whole && !Number.isInteger(value))) {
    return { ok: false, error: `${what}: ${whole ? "a whole number" : "a number"}` };
  }
  return { ok: true, value };
}

const DECIMAL = /^\d+(\.\d+)?$/;

/** The variant's body, or the first field it cannot send. */
export function variantBody(fields: readonly Field[], draft: Draft): Parsed<VariantBody> {
  const label = draft.label.trim();
  const sets: NonNullable<VariantBody["sets"]> = [];
  for (const field of fields) {
    const text = draft.values[field.path];
    if (text === undefined || text === field.initial) continue;
    if (text.trim() === "") {
      // Empty turns an optional setting off; any other setting stays the version's.
      if (field.nullable && field.initial !== "") sets.push({ path: field.path, value: null });
      continue;
    }
    const what = `${field.group} ${field.name}`;
    if (field.kind === "switch") {
      sets.push({ path: field.path, value: text === "true" });
      continue;
    }
    const parsed = parseNumber(text, field.whole, what);
    if (!parsed.ok) return parsed;
    if (field.initial !== "" && Number(field.initial) === parsed.value) continue;
    sets.push({ path: field.path, value: parsed.value });
  }
  for (const rule of draft.added) {
    const value: Record<string, unknown> = { rule };
    for (const [name, start] of Object.entries(ADDABLE[rule] ?? {})) {
      const text = draft.values[`+${rule}.${name}`] ?? String(start);
      const parsed = parseNumber(text, WHOLE.has(name), `${rule} ${name}`);
      if (!parsed.ok) return parsed;
      value[name] = parsed.value;
    }
    sets.push({ path: "exits[+]", value });
  }
  const body: VariantBody = { label: label || "run", sets };
  for (const [key, text, what] of [
    ["sleeve_weight", draft.sleeveWeight, "Sleeve weight"],
    ["risk_pct", draft.riskPct, "Risk per trade"],
  ] as const) {
    if (text.trim() === "") continue;
    if (!DECIMAL.test(text.trim())) return { ok: false, error: `${what}: a decimal, e.g. 0.5` };
    body[key] = text.trim();
  }
  if (draft.maxPositions.trim() !== "") {
    const parsed = parseNumber(draft.maxPositions, true, "Max positions");
    if (!parsed.ok) return parsed;
    body.max_positions = parsed.value;
  }
  if (draft.slotOrder === "random") {
    const parsed = parseNumber(draft.seed, true, "Random slot order seed");
    if (!parsed.ok) return parsed;
    body.slot_order = `random:${parsed.value}`;
  } else if (draft.slotOrder !== "time") {
    body.slot_order = draft.slotOrder;
  }
  return { ok: true, value: body };
}

export type StartForm = Readonly<{
  versionId: string;
  universeId: string;
  start: string;
  end: string;
  drafts: readonly Draft[];
}>;

/** The start request: one draft is a plain run, two or more a sweep with unique labels. */
export function startBody(version: VersionOption | undefined, form: StartForm): Parsed<StartBody> {
  if (!version) return { ok: false, error: "Choose a strategy version" };
  if (!form.universeId) return { ok: false, error: "Choose a universe" };
  if (!form.start || !form.end || form.start >= form.end) {
    return { ok: false, error: "The window needs a start before its end" };
  }
  const fields = versionFields(version);
  const variants: VariantBody[] = [];
  for (const draft of form.drafts) {
    const parsed = variantBody(fields, draft);
    if (!parsed.ok)
      return form.drafts.length > 1
        ? { ok: false, error: `${draft.label}: ${parsed.error}` }
        : parsed;
    variants.push(parsed.value);
  }
  const labels = variants.map((variant) => variant.label);
  if (variants.length > 1 && new Set(labels).size !== labels.length) {
    return { ok: false, error: "Each variant needs its own label" };
  }
  return {
    ok: true,
    value: {
      strategy_version_id: version.id,
      universe_id: form.universeId,
      start: form.start,
      end: form.end,
      variants,
    },
  };
}

/** A JSON value as text: strings as they are, numbers and booleans printed, the rest as JSON. */
export function asText(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value) ?? "";
}
