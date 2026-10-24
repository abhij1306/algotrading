import { LogOut, RotateCw } from "lucide-react";
import { Badge } from "@/components/atoms/badge";
import { Button } from "@/components/atoms/button";
import { StatusDot } from "@/components/atoms/status-dot";

/** The last login or disconnect failure, under the header control. */
function LoginError({ error }: Readonly<{ error: string }>) {
  if (!error) return null;
  return (
    <span
      role="alert"
      className="absolute top-full right-0 z-50 mt-2 w-72 rounded-md bg-background p-3 text-xs text-loss shadow-lg"
    >
      {error}
    </span>
  );
}

/** The first status read, or a manual retry, is in flight. */
export function LoginChecking({ name }: Readonly<{ name: string }>) {
  return (
    <span className="inline-flex items-center gap-2 text-sm text-foreground-muted">
      <StatusDot spinning className="size-3" />
      Checking {name}
    </span>
  );
}

/** The backend is unreachable or failing; the button re-reads the status now. */
export function LoginOffline({ onRetry }: Readonly<{ onRetry: () => void }>) {
  return (
    <span className="inline-flex items-center gap-1 text-sm">
      <Badge tone="loss" title="The backend did not answer the login status read">
        Backend offline
      </Badge>
      <Button size="icon" onClick={onRetry} aria-label="Retry the login status">
        <RotateCw className="size-4" />
      </Button>
    </span>
  );
}

/** The backend answered but could not read the saved login; logging in again will not fix it. */
export function LoginUnreadable({ name, error }: Readonly<{ name: string; error: string }>) {
  return (
    <Badge tone="warning" className="text-sm" title={error}>
      {name} status unreadable
    </Badge>
  );
}

/** The broker session is live: who is logged in, and a disconnect button. */
export function LoginConnected({
  name,
  userId,
  disabled,
  error,
  onDisconnect,
}: Readonly<{
  name: string;
  userId: string;
  disabled: boolean;
  error: string;
  onDisconnect: () => void;
}>) {
  return (
    <span className="relative inline-flex items-center gap-1 text-sm">
      <Badge tone="profit">
        {name}
        {userId ? <span className="font-mono text-foreground-secondary">{userId}</span> : null}
      </Badge>
      <Button
        size="icon"
        onClick={onDisconnect}
        disabled={disabled}
        aria-label={`Disconnect from ${name}`}
        title={`Disconnect from ${name}`}
      >
        <LogOut className="size-4" />
      </Button>
      <LoginError error={error} />
    </span>
  );
}

/** Not connected: the button that starts the login, and the last error under it. */
export function LoginConnect({
  name,
  disabled,
  error,
  onConnect,
}: Readonly<{ name: string; disabled: boolean; error: string; onConnect: () => void }>) {
  return (
    <span className="relative inline-flex">
      <Button size="sm" variant="primary" onClick={onConnect} disabled={disabled}>
        Connect {name}
      </Button>
      <LoginError error={error} />
    </span>
  );
}
