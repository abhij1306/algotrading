import { useState } from "react";
import { getFlashClassName, nextFlash, type FlashState } from "./screener-view";

/**
 * Flash class for a displayed value: when the value changes between renders the cell glows up
 * or down. The phase alternates so back-to-back changes restart the CSS animation; the class can
 * stay on afterwards because the animation does not repeat.
 */
export function useFlash(value: number): string | undefined {
  const [seen, setSeen] = useState<{ value: number; flash?: FlashState }>({ value });
  if (seen.value === value) return getFlashClassName(seen.flash);
  const flash = nextFlash(seen.value, value, seen.flash);
  // Storing information from the previous render (adjusting state during render, not in an effect).
  setSeen({ value, flash });
  return getFlashClassName(flash);
}
