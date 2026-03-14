import { useState, useEffect } from "react";
import { LogOut, Key, CheckCircle, AlertCircle, X, Wifi, WifiOff } from "lucide-react";
import { apiClient } from "@/lib/api-client";
import { getErrorMessage } from "@/lib/type-guards";

export default function LoginButton({ collapsed = false }: Readonly<{ collapsed?: boolean }>) {
  const [connected, setConnected] = useState(false);
  const [userId, setUserId] = useState("");
  const [loading, setLoading] = useState(false);
  const [checking, setChecking] = useState(true);
  const [showModal, setShowModal] = useState(false);
  const [authCode, setAuthCode] = useState("");
  const [status, setStatus] = useState<"idle" | "success" | "error">("idle");
  const [message, setMessage] = useState("");
  const [systemOffline, setSystemOffline] = useState(false);

  // Check connection status on mount
  useEffect(() => {
    checkConnection();
  }, []);

  const checkConnection = async () => {
    setChecking(true);
    setSystemOffline(false);
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 5000); // Reduced to 5s for faster feedback

    try {
      const result = await apiClient.get<{ connected: boolean; user_id?: string }>(
        "/api/auth/fyers/status"
      );
      if (result.error) throw new Error(result.error.message || "Backend error");
      const data = result.data;

      if (data?.connected) {
        setConnected(true);
        setUserId(data?.user_id || "");
      } else {
        setConnected(false);
      }
    } catch {
      // Silently handle offline/network errors
      setConnected(false);
      setSystemOffline(true);
    } finally {
      clearTimeout(timeoutId);
      setChecking(false);
    }
  };

  const handleDisconnect = async () => {
    if (!confirm("Disconnect from Fyers? You will need to re-authenticate.")) return;

    setLoading(true);
    try {
      const result = await apiClient.post("/api/auth/fyers/disconnect");
      if (result.data) {
        setConnected(false);
        setUserId("");
        setMessage("Disconnected successfully");
        setStatus("success");
        setTimeout(() => setStatus("idle"), 2000);
      }
    } catch (error) {
      console.error("[Fyers] Disconnect error:", error);
      setStatus("error");
      setMessage("Failed to disconnect");
    } finally {
      setLoading(false);
    }
  };

  const handleLoginStart = async () => {
    setLoading(true);
    setStatus("idle");
    setMessage("");

    try {
      const result = await apiClient.get<{ url: string; detail?: string; error?: string }>(
        "/api/auth/fyers/url"
      );
      const data = result.data;

      if (data?.url) {
        // Use Electron's shell.openExternal if available
        const electron = (
          globalThis as unknown as { electron?: { openExternal?: (_url: string) => void } }
        ).electron;
        if (electron?.openExternal) {
          electron.openExternal(data.url);
        } else {
          const openBrowserWindow = globalThis.window?.open;
          if (typeof openBrowserWindow !== "function") {
            setStatus("error");
            setMessage("Login URL generated but no browser window is available to open it.");
            return;
          }
          openBrowserWindow(data.url, "_blank", "width=800,height=600");
        }
        setShowModal(true);
      } else {
        setStatus("error");
        setMessage(result.error?.message || data?.detail || data?.error || "Failed to get Login URL");
      }
    } catch (e: unknown) {
      console.error("[Fyers] Network error:", e);
      setStatus("error");
      setMessage(`Network Error: ${getErrorMessage(e)}`);
    } finally {
      setLoading(false);
    }
  };

  const handleSubmit = async () => {
    if (!authCode) return;
    setLoading(true);
    setStatus("idle");

    try {
      const result = await apiClient.post("/api/auth/fyers/token", { auth_code: authCode });
      if (result.data) {
        setStatus("success");
        setMessage("Connected successfully!");
        setConnected(true);
        setTimeout(() => {
          setShowModal(false);
          setAuthCode("");
          checkConnection(); // Refresh connection status
        }, 2000);
      } else {
        setStatus("error");
        setMessage(result.error?.message || "Authentication failed");
      }
    } catch (e: unknown) {
      console.error("[Fyers] Token network error:", e);
      setStatus("error");
      setMessage(`Failed to exchange token: ${getErrorMessage(e)}`);
    } finally {
      setLoading(false);
    }
  };

  if (checking) {
    return collapsed ? (
      <div className="flex justify-center w-full py-2">
        <div className="w-4 h-4 border-2 border-primary border-t-transparent rounded-full animate-spin" />
      </div>
    ) : (
      <div className="flex items-center gap-2 px-3 py-1.5 bg-base border border-border-subtle rounded-lg">
        <div className="w-3 h-3 border-2 border-primary border-t-transparent rounded-full animate-spin" />
        <span className="text-xs text-foreground-muted">Checking...</span>
      </div>
    );
  }

  if (systemOffline) {
    return collapsed ? (
      <div className="flex justify-center w-full py-2" title="System Offline">
        <WifiOff className="w-4 h-4 text-loss opacity-50" />
      </div>
    ) : (
      <div
        className="flex items-center gap-2 px-3 py-1.5 bg-elevated/50 border border-border rounded-lg cursor-not-allowed opacity-75"
        title="Backend unreachable"
      >
        <WifiOff className="w-3.5 h-3.5 text-foreground-disabled" />
        <span className="text-xs font-semibold text-foreground-disabled">System Offline</span>
        <button onClick={checkConnection} className="ml-1 hover:text-foreground transition-colors">
          <div className="w-2 h-2 rounded-full bg-loss/20 hover:bg-loss" />
        </button>
      </div>
    );
  }

  if (connected) {
    return collapsed ? (
      <div className="flex flex-col items-center gap-2">
        <div className="flex justify-center w-full py-2" title={`Connected as ${userId}`}>
          <Wifi className="w-4 h-4 text-profit" />
        </div>
        <button
          onClick={handleDisconnect}
          className="p-1 hover:bg-loss/10 rounded text-foreground-muted hover:text-loss"
          title="Disconnect"
        >
          <LogOut className="w-3 h-3" />
        </button>
      </div>
    ) : (
      <div className="flex items-center justify-between w-full px-3 py-2 bg-base border border-profit/30 rounded-lg group hover:border-profit/50 transition-colors">
        <div className="flex items-center gap-2.5 overflow-hidden">
          <div className="relative">
            <Wifi className="w-4 h-4 text-profit" />
            <div className="absolute inset-0 bg-profit blur-sm opacity-20" />
          </div>
          <div className="flex flex-col truncate">
            <span className="text-xxs font-semibold text-profit uppercase tracking-wider">
              Connected
            </span>
            {userId && (
              <span className="text-xxs text-foreground-muted font-mono truncate">{userId}</span>
            )}
          </div>
        </div>
        <button
          onClick={handleDisconnect}
          disabled={loading}
          className="p-1.5 rounded-md hover:bg-loss/10 text-foreground-muted hover:text-loss transition-colors"
          title="Disconnect from Fyers"
        >
          <LogOut className="w-3.5 h-3.5" />
        </button>
      </div>
    );
  }

  return (
    <>
      <div className="relative w-full">
        <button
          onClick={handleLoginStart}
          disabled={loading}
          title={collapsed ? "Connect Fyers" : ""}
          className={
            collapsed
              ? "flex items-center justify-center w-full py-3 text-foreground-muted hover:text-primary"
              : "w-full flex items-center justify-between px-3 py-2 bg-base border border-border-subtle hover:border-primary/50 rounded-lg text-xs font-semibold text-foreground-muted hover:text-primary transition-all group disabled:opacity-50"
          }
        >
          <div className="flex items-center gap-2">
            <WifiOff className="w-4 h-4 group-hover:scale-110 transition-transform" />
            {!collapsed && <span>Connect Fyers</span>}
          </div>
        </button>

        {status === "error" && !showModal && message && (
          <div className="absolute top-full left-0 mt-2 w-64 p-2 bg-loss/10 border border-loss/20 rounded text-xs text-loss z-50">
            <AlertCircle className="w-3 h-3 inline mr-1" />
            {message}
          </div>
        )}
      </div>

      {showModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-void/80 backdrop-blur-sm">
          <div className="bg-base border border-border-subtle rounded-xl p-6 w-[500px] shadow-2xl relative">
            <button
              onClick={() => setShowModal(false)}
              className="absolute top-3 right-3 text-foreground-muted hover:text-foreground"
            >
              <X className="w-4 h-4" />
            </button>

            <h3 className="text-lg font-semibold text-foreground mb-2 flex items-center gap-2">
              <Key className="w-5 h-5 text-primary" /> Authenticate Fyers
            </h3>

            <div className="bg-primary/5 border border-primary/10 rounded-lg p-4 mb-4 text-xs text-primary leading-relaxed space-y-2">
              <p>
                <strong>Step 1:</strong> A Fyers login window has opened in your browser
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
                    https://example.com/?{"{"}auth_code=
                    <span className="text-warning font-semibold">XXXXXX...</span>
                    {"}"}
                  </code>
                </span>
              </p>
              <p>
                <strong>Step 4:</strong> Copy only the{" "}
                <code className="bg-warning/10 text-warning px-1 rounded">auth_code</code> value and
                paste below
              </p>
            </div>

            <div className="space-y-4">
              <div>
                <label htmlFor="fyers-auth-code" className="block text-xs font-semibold text-foreground-muted mb-2">
                  Authorization Code
                </label>
                <input
                  id="fyers-auth-code"
                  type="text"
                  value={authCode}
                  onChange={(e) => setAuthCode(e.target.value)}
                  placeholder="Paste auth_code here (without auth_code= prefix)"
                  className="w-full bg-void/50 border border-border-subtle rounded-lg px-4 py-3 text-sm text-foreground font-mono focus:border-primary/50 outline-none"
                  autoFocus
                />
              </div>

              <button
                onClick={handleSubmit}
                disabled={loading || !authCode}
                className="w-full py-3 bg-gradient-to-r from-primary to-primary-bright rounded-lg text-sm font-semibold text-white hover:from-primary/90 hover:to-primary-bright/90 disabled:opacity-50 disabled:cursor-not-allowed transition-all"
              >
                {loading ? "Verifying..." : "Authenticate"}
              </button>
            </div>

            {status === "success" && (
              <div className="mt-4 p-3 bg-profit/10 border border-profit/20 rounded flex items-center gap-2 text-profit text-xs font-semibold">
                <CheckCircle className="w-4 h-4" /> {message}
              </div>
            )}

            {status === "error" && (
              <div className="mt-4 p-3 bg-loss/10 border border-loss/20 rounded flex items-center gap-2 text-loss text-xs font-semibold">
                <AlertCircle className="w-4 h-4" /> {message}
              </div>
            )}
          </div>
        </div>
      )}
    </>
  );
}
