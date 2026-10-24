/** Callback ref: opens a `<dialog>` as a modal when it mounts (focus moves into it, Esc closes). */
export function openModal(dialog: HTMLDialogElement | null): void {
  if (dialog !== null && !dialog.open) dialog.showModal();
}
