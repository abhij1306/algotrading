/** Callback ref: moves focus to the element when it mounts (e.g. the first field of a modal). */
export function focusOnMount(element: HTMLElement | null): void {
  element?.focus();
}
