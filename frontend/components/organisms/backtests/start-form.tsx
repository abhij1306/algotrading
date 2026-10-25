import { useState, type FormEvent, type ReactNode } from "react";
import { Plus, X } from "lucide-react";
import { Button } from "@/components/atoms/button";
import { fieldClass } from "@/components/atoms/field";
import { Text } from "@/components/atoms/text";
import {
  ADDABLE,
  addableRules,
  copyDraft,
  newDraft,
  startBody,
  versionFields,
  type Draft,
  type Field,
  type VersionOption,
} from "@/lib/backtest-form";
import { useBacktestOptions, useStartBacktest } from "@/lib/queries/backtests";
import { cn } from "@/lib/utils";

/** The default window: the five years to today. */
function defaultWindow(today: Date = new Date()): { start: string; end: string } {
  const end = today.toISOString().slice(0, 10);
  return { start: `${Number(end.slice(0, 4)) - 5}${end.slice(4)}`, end };
}

function Labelled({
  label,
  hint,
  className,
  children,
}: Readonly<{ label: string; hint?: string; className?: string; children: ReactNode }>) {
  return (
    <label className={cn("flex flex-col gap-1 text-xs text-foreground-muted", className)}>
      <span>
        {label}
        {hint ? <span className="ml-1 opacity-75">({hint})</span> : null}
      </span>
      {children}
    </label>
  );
}

type Edit = (change: (draft: Draft) => Draft) => void;

function PolicyFields({ draft, edit }: Readonly<{ draft: Draft; edit: Edit }>) {
  return (
    <div className="flex flex-wrap gap-4">
      <Labelled label="Sleeve weight" hint="0-1, blank: policy">
        <input
          className={cn(fieldClass, "w-28")}
          inputMode="decimal"
          value={draft.sleeveWeight}
          onChange={(e) => edit((d) => ({ ...d, sleeveWeight: e.target.value }))}
        />
      </Labelled>
      <Labelled label="Risk per trade" hint="fraction, e.g. 0.005">
        <input
          className={cn(fieldClass, "w-28")}
          inputMode="decimal"
          value={draft.riskPct}
          onChange={(e) => edit((d) => ({ ...d, riskPct: e.target.value }))}
        />
      </Labelled>
      <Labelled label="Max positions" hint="blank: policy">
        <input
          className={cn(fieldClass, "w-24")}
          inputMode="numeric"
          value={draft.maxPositions}
          onChange={(e) => edit((d) => ({ ...d, maxPositions: e.target.value }))}
        />
      </Labelled>
      <Labelled label="Slot order">
        <select
          className={fieldClass}
          value={draft.slotOrder}
          onChange={(e) => edit((d) => ({ ...d, slotOrder: e.target.value }))}
        >
          <option value="time">time</option>
          <option value="score">score</option>
          <option value="random">random</option>
        </select>
      </Labelled>
      {draft.slotOrder === "random" ? (
        <Labelled label="Seed">
          <input
            className={cn(fieldClass, "w-20")}
            inputMode="numeric"
            value={draft.seed}
            onChange={(e) => edit((d) => ({ ...d, seed: e.target.value }))}
          />
        </Labelled>
      ) : null}
    </div>
  );
}

function SettingInput({
  field,
  draft,
  edit,
}: Readonly<{ field: Field; draft: Draft; edit: Edit }>) {
  const value = draft.values[field.path] ?? field.initial;
  const set = (text: string) =>
    edit((d) => ({ ...d, values: { ...d.values, [field.path]: text } }));
  if (field.kind === "switch") {
    return (
      <label className="flex items-center gap-2 text-xs text-foreground-muted">
        <input
          type="checkbox"
          className="size-4 accent-accent"
          checked={value === "true"}
          onChange={(e) => set(e.target.checked ? "true" : "false")}
        />
        <span>enabled</span>
      </label>
    );
  }
  return (
    <Labelled label={field.name}>
      <input
        className={cn(fieldClass, "w-24")}
        inputMode={field.whole ? "numeric" : "decimal"}
        placeholder="off"
        value={value}
        onChange={(e) => set(e.target.value)}
      />
    </Labelled>
  );
}

function ExitFields({
  version,
  draft,
  edit,
}: Readonly<{ version: VersionOption; draft: Draft; edit: Edit }>) {
  const groups = new Map<string, Field[]>();
  for (const field of versionFields(version)) {
    const key = field.path.replace(/\.[a-z_]+$/, "");
    const list = groups.get(key) ?? [];
    list.push(field);
    groups.set(key, list);
  }
  const addable = addableRules(version);
  return (
    <div className="space-y-4">
      <div className="grid gap-x-8 gap-y-4 md:grid-cols-2">
        {[...groups].map(([key, fields]) => (
          <fieldset key={key} className="min-w-0">
            <legend className="mb-1 text-sm">{fields[0]?.group}</legend>
            <div className="flex flex-wrap items-end gap-3">
              {fields.map((field) => (
                <SettingInput key={field.path} field={field} draft={draft} edit={edit} />
              ))}
            </div>
          </fieldset>
        ))}
        {draft.added.map((rule) => (
          <fieldset key={rule} className="min-w-0">
            <legend className="mb-1 flex items-center gap-2 text-sm">
              {rule} (added)
              <Button
                size="sm"
                aria-label={`Remove ${rule}`}
                onClick={() => edit((d) => ({ ...d, added: d.added.filter((r) => r !== rule) }))}
              >
                <X className="size-3" />
              </Button>
            </legend>
            <div className="flex flex-wrap items-end gap-3">
              {Object.entries(ADDABLE[rule] ?? {}).map(([name, start]) => {
                const key = `+${rule}.${name}`;
                return (
                  <Labelled key={key} label={name}>
                    <input
                      className={cn(fieldClass, "w-24")}
                      inputMode="decimal"
                      value={draft.values[key] ?? String(start)}
                      onChange={(e) =>
                        edit((d) => ({ ...d, values: { ...d.values, [key]: e.target.value } }))
                      }
                    />
                  </Labelled>
                );
              })}
            </div>
          </fieldset>
        ))}
      </div>
      {addable.some((rule) => !draft.added.includes(rule)) ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-foreground-muted">Add an exit:</span>
          {addable
            .filter((rule) => !draft.added.includes(rule))
            .map((rule) => (
              <Button
                key={rule}
                size="sm"
                onClick={() => edit((d) => ({ ...d, added: [...d.added, rule] }))}
              >
                <Plus className="size-3" />
                {rule}
              </Button>
            ))}
        </div>
      ) : null}
    </div>
  );
}

function StartFormBody({
  versions,
  maxVariants,
  onDone,
}: Readonly<{ versions: VersionOption[]; maxVariants: number; onDone: () => void }>) {
  const start = useStartBacktest();
  const [versionId, setVersionId] = useState(versions[0]?.id ?? "");
  const [universeId, setUniverseId] = useState("");
  const [period, setPeriod] = useState(defaultWindow);
  const [drafts, setDrafts] = useState<Draft[]>([newDraft("base")]);
  const [active, setActive] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const version = versions.find((v) => v.id === versionId);
  const universe = universeId || version?.universes[0]?.id || "";
  const draft = drafts[Math.min(active, drafts.length - 1)] ?? newDraft("base");
  const edit: Edit = (change) =>
    setDrafts((old) => old.map((d, index) => (index === active ? change(d) : d)));

  const chooseVersion = (id: string) => {
    // The settings' paths are the version's: a new version starts its variants afresh.
    setVersionId(id);
    setUniverseId("");
    setDrafts((old) => old.map((d) => ({ ...newDraft(d.label) })));
  };
  const addVariant = () => {
    setDrafts((old) => [...old, copyDraft(draft, `variant ${old.length + 1}`)]);
    setActive(drafts.length);
  };
  const removeVariant = (index: number) => {
    setDrafts((old) => old.filter((_, i) => i !== index));
    setActive(0);
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const body = startBody(version, { versionId, universeId: universe, ...period, drafts });
    if (!body.ok) {
      setError(body.error);
      return;
    }
    setError(null);
    start.mutate(body.value, { onSuccess: onDone });
  };

  return (
    <form onSubmit={submit} className="space-y-8">
      <div className="flex flex-wrap gap-4">
        <Labelled label="Strategy version">
          <select
            className={fieldClass}
            value={versionId}
            onChange={(e) => chooseVersion(e.target.value)}
          >
            {versions.map((v) => (
              <option key={v.id} value={v.id}>
                {v.strategy} v{v.version} ({v.status})
              </option>
            ))}
          </select>
        </Labelled>
        <Labelled label="Universe">
          <select
            className={fieldClass}
            value={universe}
            onChange={(e) => setUniverseId(e.target.value)}
          >
            {(version?.universes ?? []).map((u) => (
              <option key={u.id} value={u.id}>
                {u.name}
              </option>
            ))}
          </select>
        </Labelled>
        <Labelled label="From">
          <input
            type="date"
            className={fieldClass}
            value={period.start}
            onChange={(e) => setPeriod((w) => ({ ...w, start: e.target.value }))}
          />
        </Labelled>
        <Labelled label="To">
          <input
            type="date"
            className={fieldClass}
            value={period.end}
            onChange={(e) => setPeriod((w) => ({ ...w, end: e.target.value }))}
          />
        </Labelled>
      </div>

      <div className="space-y-5">
        <div className="flex flex-wrap items-center gap-2" role="tablist" aria-label="Variants">
          {drafts.map((d, index) => (
            <span key={d.id} className="flex items-center">
              <Button
                size="sm"
                role="tab"
                aria-selected={index === active}
                variant={index === active ? "primary" : "ghost"}
                onClick={() => setActive(index)}
              >
                {d.label || `variant ${index + 1}`}
              </Button>
              {drafts.length > 1 ? (
                <Button
                  size="sm"
                  aria-label={`Remove ${d.label}`}
                  onClick={() => removeVariant(index)}
                >
                  <X className="size-3" />
                </Button>
              ) : null}
            </span>
          ))}
          {drafts.length < maxVariants ? (
            <Button size="sm" onClick={addVariant}>
              <Plus className="size-3" /> Add variant
            </Button>
          ) : null}
          <span className="text-xs text-foreground-muted">
            {drafts.length > 1
              ? `A sweep of ${drafts.length}: each variant counts as a trial of the family.`
              : "One run; add a variant for a sweep (each variant is a trial)."}
          </span>
        </div>
        {drafts.length > 1 ? (
          <Labelled label="Variant label">
            <input
              className={cn(fieldClass, "w-60")}
              value={draft.label}
              onChange={(e) => edit((d) => ({ ...d, label: e.target.value }))}
            />
          </Labelled>
        ) : null}
        <PolicyFields draft={draft} edit={edit} />
        {version ? <ExitFields version={version} draft={draft} edit={edit} /> : null}
      </div>

      <div className="flex flex-wrap items-center gap-4">
        <Button type="submit" variant="primary" disabled={start.isPending}>
          {drafts.length > 1 ? `Start sweep of ${drafts.length}` : "Start run"}
        </Button>
        <Button onClick={onDone}>Cancel</Button>
        {(error ?? start.error) ? (
          <Text role="alert" tone="loss">
            {error ?? `Not started: ${start.error?.message}`}
          </Text>
        ) : null}
      </div>
    </form>
  );
}

/** The form that starts a research run or a small sweep as a background job. */
export function StartForm({ onDone }: Readonly<{ onDone: () => void }>) {
  const options = useBacktestOptions();
  if (options.error) {
    return (
      <Text role="alert" tone="loss">
        Could not load the strategy versions: {options.error.message}
      </Text>
    );
  }
  if (!options.data) return <Text tone="muted">Loading…</Text>;
  if (options.data.versions.length === 0) return <Text tone="muted">No strategy versions.</Text>;
  return (
    <StartFormBody
      versions={options.data.versions}
      maxVariants={options.data.max_variants}
      onDone={onDone}
    />
  );
}
