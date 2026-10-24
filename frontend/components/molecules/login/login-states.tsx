import { AlertCircle, LogOut, Wifi, WifiOff } from "lucide-react";
import { StatusDot } from "@/components/atoms/status-dot";

/** The first status read is in flight. */
export function LoginChecking() {
  return (
    <div className="flex items-center gap-2 px-3 py-1.5 bg-base border border-border-subtle rounded-lg">
      <StatusDot spinning className="w-3 h-3" />
      <span className="text-xs text-foreground-muted">Checking...</span>
    </div>
  );
}

/** The backend is unreachable; the dot retries the status read. */
export function LoginOffline({ onRetry }: Readonly<{ onRetry: () => void }>) {
  return (
    <div
      className="flex items-center gap-2 px-3 py-1.5 bg-elevated/50 border border-border rounded-lg cursor-not-allowed opacity-75"
      title="Backend unreachable"
    >
      <WifiOff className="w-3.5 h-3.5 text-foreground-disabled" />
      <span className="text-xs font-semibold text-foreground-disabled">System Offline</span>
      <button onClick={onRetry} className="ml-1 hover:text-foreground transition-colors">
        <StatusDot className="w-2 h-2 bg-loss/20 hover:bg-loss" />
      </button>
    </div>
  );
}

/** The broker session is live: who is logged in, and a disconnect button. */
export function LoginConnected({
  name,
  label,
  userId,
  disabled,
  onDisconnect,
}: Readonly<{
  name: string;
  label: string;
  userId: string;
  disabled: boolean;
  onDisconnect: () => void;
}>) {
  return (
    <div className="flex items-center justify-between w-full px-3 py-2 bg-base border border-profit/30 rounded-lg group hover:border-profit/50 transition-colors">
      <div className="flex items-center gap-2.5 overflow-hidden">
        <div className="relative">
          <Wifi className="w-4 h-4 text-profit" />
          <div className="absolute inset-0 bg-profit blur-sm opacity-20" />
        </div>
        <div className="flex flex-col truncate">
          <span className="text-xxs font-semibold text-profit uppercase tracking-wider">
            {label}
          </span>
          {userId && (
            <span className="text-xxs text-foreground-muted font-mono truncate">{userId}</span>
          )}
        </div>
      </div>
      <button
        onClick={onDisconnect}
        disabled={disabled}
        className="p-1.5 rounded-md hover:bg-loss/10 text-foreground-muted hover:text-loss transition-colors"
        title={`Disconnect from ${name}`}
      >
        <LogOut className="w-3.5 h-3.5" />
      </button>
    </div>
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
    <div className="relative w-full">
      <button
        onClick={onConnect}
        disabled={disabled}
        className="w-full flex items-center justify-between px-3 py-2 bg-base border border-border-subtle hover:border-primary/50 rounded-lg text-xs font-semibold text-foreground-muted hover:text-primary transition-all group disabled:opacity-50"
      >
        <div className="flex items-center gap-2">
          <WifiOff className="w-4 h-4 group-hover:scale-110 transition-transform" />
          <span>Connect {name}</span>
        </div>
      </button>

      {error && (
        <div className="absolute top-full left-0 mt-2 w-64 p-2 bg-loss/10 border border-loss/20 rounded text-xs text-loss z-50">
          <AlertCircle className="w-3 h-3 inline mr-1" />
          {error}
        </div>
      )}
    </div>
  );
}
