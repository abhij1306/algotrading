/** A debounced function plus `cancel()` to drop a pending call. */
export type Debounced<Args extends unknown[]> = ((...args: Args) => void) & { cancel: () => void };

/**
 * Debounce utility for delaying function execution
 * @param func Function to debounce
 * @param wait Delay in milliseconds
 * @returns Debounced function; call `.cancel()` to drop a pending invocation
 */
export function debounce<T extends (...args: unknown[]) => unknown>(
  func: T,
  wait: number,
): Debounced<Parameters<T>> {
  let timeout: ReturnType<typeof setTimeout> | null = null;

  const cancel = () => {
    if (timeout) {
      clearTimeout(timeout);
      timeout = null;
    }
  };

  const executedFunction = (...args: Parameters<T>) => {
    cancel();
    timeout = setTimeout(() => {
      timeout = null;
      func(...args);
    }, wait);
  };

  return Object.assign(executedFunction, { cancel });
}
