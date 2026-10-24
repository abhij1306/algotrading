import { X } from "lucide-react";
import { Button } from "@/components/atoms/button";
import { Text } from "@/components/atoms/text";
import { openModal } from "@/lib/focus";
import { AuthSteps } from "./auth-steps";

export type AuthOutcome = "idle" | "success" | "error";

/** The modal that takes the code from the broker's redirect and submits it. */
export function AuthCodeDialog({
  inputId,
  name,
  code,
  authCode,
  loading,
  outcome,
  message,
  onChange,
  onSubmit,
  onClose,
}: Readonly<{
  inputId: string;
  name: string;
  code: string;
  authCode: string;
  loading: boolean;
  outcome: AuthOutcome;
  message: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onClose: () => void;
}>) {
  return (
    <dialog
      ref={openModal}
      onClose={onClose}
      aria-labelledby={`${inputId}-title`}
      className="m-auto w-[calc(100%-2rem)] max-w-lg rounded-xl bg-background p-6 text-foreground shadow-xl"
    >
      <div className="mb-4 flex items-start justify-between gap-4">
        <h2 id={`${inputId}-title`} className="text-lg">
          Log in to {name}
        </h2>
        <Button size="icon" onClick={onClose} aria-label="Close">
          <X className="size-4" />
        </Button>
      </div>

      <AuthSteps name={name} code={code} />

      <form
        method="dialog"
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit();
        }}
      >
        <label htmlFor={inputId} className="block text-xs text-foreground-muted">
          Authorization Code
        </label>
        <input
          id={inputId}
          type="text"
          autoComplete="off"
          autoFocus
          value={authCode}
          onChange={(e) => onChange(e.target.value)}
          placeholder={`Paste ${code} here (without ${code}= prefix)`}
          className="w-full rounded-md bg-subtle px-3 py-2.5 font-mono text-sm text-foreground outline-none placeholder:text-foreground-muted focus-visible:outline-2 focus-visible:outline-accent"
        />
        <Button type="submit" variant="primary" className="w-full" disabled={loading || !authCode}>
          {loading ? "Verifying…" : "Authenticate"}
        </Button>
      </form>

      {outcome === "idle" ? null : (
        <Text role="status" tone={outcome === "error" ? "loss" : "default"} className="mt-4">
          {message}
        </Text>
      )}
    </dialog>
  );
}
