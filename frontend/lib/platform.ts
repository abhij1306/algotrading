/** Platform detection for keyboard shortcuts: ⌘ on Apple devices, Ctrl everywhere else. */
import { isObject } from "@/lib/type-guards";

const APPLE_PLATFORM = /mac|iphone|ipad|ipod/i;

function stringProperty(value: unknown, key: string): string | undefined {
  if (!isObject(value)) return undefined;
  const property = value[key];
  return typeof property === "string" && property !== "" ? property : undefined;
}

/** `navigator.userAgentData.platform` where supported, else the legacy `navigator.platform`. */
function platformName(): string {
  // Read through `unknown`: `userAgentData` is not in every DOM lib and `platform` is deprecated.
  const nav: unknown = globalThis.navigator;
  if (typeof nav !== "object" || nav === null) return "";
  const legacy: unknown = Reflect.get(nav, "platform");
  return (
    stringProperty(Reflect.get(nav, "userAgentData"), "platform") ??
    (typeof legacy === "string" ? legacy : "")
  );
}

export function isApplePlatform(): boolean {
  return APPLE_PLATFORM.test(platformName());
}

/** The modifier key's label: "⌘" on Apple platforms, "Ctrl" elsewhere. */
export function modifierKeyLabel(): string {
  return isApplePlatform() ? "⌘" : "Ctrl";
}

/** A modifier shortcut's label, e.g. "⌘K" on macOS and "Ctrl+K" on Windows/Linux. */
export function formatShortcut(key: string): string {
  return isApplePlatform() ? `⌘${key}` : `Ctrl+${key}`;
}

/** True for the platform's modifier (⌘ on Apple, Ctrl elsewhere) plus `key`, without Alt. */
export function isModifierShortcut(event: KeyboardEvent, key: string): boolean {
  const modifier = isApplePlatform() ? event.metaKey : event.ctrlKey;
  return modifier && !event.altKey && event.key.toLowerCase() === key.toLowerCase();
}
