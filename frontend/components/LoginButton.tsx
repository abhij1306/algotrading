import { useState } from "react";
import { LogOut, Key, CheckCircle, AlertCircle, X, Wifi, WifiOff } from "lucide-react";
import {
  type Broker,
  useBrokerDisconnect,
  useBrokerLoginUrl,
  useBrokerStatus,
  useBrokerToken,
} from "@/lib/queries/auth";
import { focusOnMount } from "@/lib/focus";

/** What the login box calls the broker and the code its redirect carries. */
const LOGIN_TEXT: Record<Broker, { name: string; code: string }> = {
  fyers: { name: "Fyers", code: "auth_code" },
  zerodha: { name: "Zerodha", code: "request_token" },
};

export default function LoginButton({
  collapsed = false,
  broker = "fyers",
}: Readonly<{ collapsed?: boolean; broker?: Broker }>) {
  const { name, code } = LOGIN_TEXT[broker];
  const statusQuery = useBrokerStatus(broker);
  const loginUrl = useBrokerLoginUrl(broker);
  const token = useBrokerToken(broker);
  const disconnect = useBrokerDisconnect(broker);
  const connected = statusQuery.data?.connected ?? false;
  const userId = statusQuery.data?.userId ?? "";
  const systemOffline = statusQuery.data?.offline ?? false;
  // Spinner on first load and on a manual retry from the offline badge, not on background refetches.
  const checking = statusQuery.isPending || (statusQuery.isFetching && systemOffline);
  const loading = loginUrl.isPending || token.isPending || disconnect.isPending;
  const [showModal, setShowModal] = useState(false);
  const [authCode, setAuthCode] = useState("");
  const [status, setStatus] = useState<"idle" | "success" | "error">("idle");
  const [message, setMessage] = useState("");

  const fail = (text: string) => {
    setStatus("error");
    setMessage(text);
  };

  const checkConnection = () => {
    void statusQuery.refetch();
  };

  const handleDisconnect = () => {
    if (!confirm(`Disconnect from ${name}? You will need to re-authenticate.`)) return;
    disconnect.mutate(undefined, {
      onSuccess: () => {
        setMessage("Disconnected successfully");
        setStatus("success");
        setTimeout(() => setStatus("idle"), 2000);
      },
      onError: (error) => fail(`Failed to disconnect: ${error.message}`),
    });
  };

  const openLoginPage = (url: string) => {
    if (typeof globalThis.window?.open !== "function") {
      fail("Login URL generated but no browser window is available to open it.");
      return;
    }
    const popup = globalThis.window.open(url, "_blank", "width=800,height=600");
    if (popup === null) {
      fail("Popup blocked; please allow popups to continue login.");
      return;
    }
    setShowModal(true);
  };

  const handleLoginStart = () => {
    setStatus("idle");
    setMessage("");
    loginUrl.mutate(undefined, {
      onSuccess: openLoginPage,
      onError: (error) => fail(error.message),
    });
  };

  const handleSubmit = () => {
    if (!authCode) return;
    setStatus("idle");
    token.mutate(authCode, {
      onSuccess: () => {
        setStatus("success");
        setMessage("Connected successfully!");
        setTimeout(() => {
          setShowModal(false);
          setAuthCode("");
        }, 2000);
      },
      onError: (error) => fail(error.message || "Authentication failed"),
    });
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
        <div className="flex justify-center w-full py-2" title={`${name}: connected as ${userId}`}>
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
              {broker === "fyers" ? "Connected" : `${name} connected`}
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
          title={`Disconnect from ${name}`}
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
          title={collapsed ? `Connect ${name}` : ""}
          className={
            collapsed
              ? "flex items-center justify-center w-full py-3 text-foreground-muted hover:text-primary"
              : "w-full flex items-center justify-between px-3 py-2 bg-base border border-border-subtle hover:border-primary/50 rounded-lg text-xs font-semibold text-foreground-muted hover:text-primary transition-all group disabled:opacity-50"
          }
        >
          <div className="flex items-center gap-2">
            <WifiOff className="w-4 h-4 group-hover:scale-110 transition-transform" />
            {!collapsed && <span>Connect {name}</span>}
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
              <Key className="w-5 h-5 text-primary" /> Authenticate {name}
            </h3>

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
                <code className="bg-warning/10 text-warning px-1 rounded">{code}</code> value and
                paste below
              </p>
            </div>

            <div className="space-y-4">
              <div>
                <label
                  htmlFor={`${broker}-auth-code`}
                  className="block text-xs font-semibold text-foreground-muted mb-2"
                >
                  Authorization Code
                </label>
                <input
                  id={`${broker}-auth-code`}
                  type="text"
                  value={authCode}
                  onChange={(e) => setAuthCode(e.target.value)}
                  placeholder={`Paste ${code} here (without ${code}= prefix)`}
                  className="w-full bg-void/50 border border-border-subtle rounded-lg px-4 py-3 text-sm text-foreground font-mono focus:border-primary/50 outline-none"
                  ref={focusOnMount}
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
