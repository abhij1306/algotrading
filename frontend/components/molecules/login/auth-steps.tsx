/** How to get the code: the four steps from the broker's login window to the paste box. */
export function AuthSteps({ name, code }: Readonly<{ name: string; code: string }>) {
  return (
    <div className="bg-primary/5 border border-primary/10 rounded-lg p-4 mb-4 text-xs text-primary leading-relaxed space-y-2">
      <p>
        <strong>Step 1:</strong> A {name} login window has opened in your browser
      </p>
      <p>
        <strong>Step 2:</strong> Login with your credentials
      </p>
      <p className="flex items-start gap-2">
        <span className="text-primary font-semibold">Step 3:</span>
        <span>
          After login, you&apos;ll be redirected to a page with URL like:
          <br />
          <code className="text-xxs bg-void/40 px-2 py-0.5 rounded mt-1 block">
            https://example.com/?{"{"}
            {code}=<span className="text-warning font-semibold">XXXXXX...</span>
            {"}"}
          </code>
        </span>
      </p>
      <p>
        <strong>Step 4:</strong> Copy only the{" "}
        <code className="bg-warning/10 text-warning px-1 rounded">{code}</code> value and paste
        below
      </p>
    </div>
  );
}
