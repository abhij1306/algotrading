import { AlertCircle, CheckCircle, Key, X } from "lucide-react";
import { focusOnMount } from "@/lib/focus";
import { AuthSteps } from "./auth-steps";

export type AuthOutcome = "idle" | "success" | "error";

const MESSAGE = {
  success: {
    Icon: CheckCircle,
    className:
      "mt-4 p-3 bg-profit/10 border border-profit/20 rounded flex items-center gap-2 text-profit text-xs font-semibold",
  },
  error: {
    Icon: AlertCircle,
    className:
      "mt-4 p-3 bg-loss/10 border border-loss/20 rounded flex items-center gap-2 text-loss text-xs font-semibold",
  },
} as const;

function AuthMessage({ outcome, message }: Readonly<{ outcome: AuthOutcome; message: string }>) {
  if (outcome === "idle") return null;
  const { Icon, className } = MESSAGE[outcome];
  return (
    <div className={className}>
      <Icon className="w-4 h-4" /> {message}
    </div>
  );
}

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
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-void/80 backdrop-blur-sm">
      <div className="bg-base border border-border-subtle rounded-xl p-6 w-[500px] shadow-2xl relative">
        <button
          onClick={onClose}
          className="absolute top-3 right-3 text-foreground-muted hover:text-foreground"
        >
          <X className="w-4 h-4" />
        </button>

        <h3 className="text-lg font-semibold text-foreground mb-2 flex items-center gap-2">
          <Key className="w-5 h-5 text-primary" /> Authenticate {name}
        </h3>

        <AuthSteps name={name} code={code} />

        <div className="space-y-4">
          <div>
            <label
              htmlFor={inputId}
              className="block text-xs font-semibold text-foreground-muted mb-2"
            >
              Authorization Code
            </label>
            <input
              id={inputId}
              type="text"
              value={authCode}
              onChange={(e) => onChange(e.target.value)}
              placeholder={`Paste ${code} here (without ${code}= prefix)`}
              className="w-full bg-void/50 border border-border-subtle rounded-lg px-4 py-3 text-sm text-foreground font-mono focus:border-primary/50 outline-none"
              ref={focusOnMount}
            />
          </div>

          <button
            onClick={onSubmit}
            disabled={loading || !authCode}
            className="w-full py-3 bg-gradient-to-r from-primary to-primary-bright rounded-lg text-sm font-semibold text-white hover:from-primary/90 hover:to-primary-bright/90 disabled:opacity-50 disabled:cursor-not-allowed transition-all"
          >
            {loading ? "Verifying..." : "Authenticate"}
          </button>
        </div>

        <AuthMessage outcome={outcome} message={message} />
      </div>
    </div>
  );
}
