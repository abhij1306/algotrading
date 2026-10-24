/** How to get the code: from the broker's login window to the paste box. */
export function AuthSteps({ name, code }: Readonly<{ name: string; code: string }>) {
  return (
    <ol className="mb-6 list-decimal space-y-1.5 pl-5 text-sm text-foreground-secondary">
      <li>Log in to {name} in the window that just opened.</li>
      <li>
        The window then lands on a page whose address holds{" "}
        <code className="font-mono text-foreground">{code}=…</code>
      </li>
      <li>
        Copy only the value after <code className="font-mono text-foreground">{code}=</code> and
        paste it below.
      </li>
    </ol>
  );
}
