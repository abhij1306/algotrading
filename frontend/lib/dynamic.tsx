import { createElement, lazy, Suspense, type ComponentType, type ReactNode } from "react";

type Loaded<P> = ComponentType<P> | { default: ComponentType<P> };

/**
 * Lazily load a component and render `loading` until it is ready.
 * Replaces next/dynamic; the loader may resolve to a component or a module with a default export.
 */
export default function dynamic<P extends object>(
  loader: () => Promise<Loaded<P>>,
  options: { loading?: () => ReactNode } = {},
): ComponentType<P> {
  const Lazy = lazy(async () => {
    const mod = await loader();
    return "default" in mod ? mod : { default: mod };
  });

  function Dynamic(props: P) {
    return (
      <Suspense fallback={options.loading?.() ?? null}>
        {createElement(Lazy as ComponentType<P>, props)}
      </Suspense>
    );
  }
  return Dynamic;
}
