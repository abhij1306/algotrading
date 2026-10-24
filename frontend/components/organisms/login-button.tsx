import { useState } from "react";
import { AuthCodeDialog, type AuthOutcome } from "@/components/molecules/login/auth-code-dialog";
import {
  LoginChecking,
  LoginConnect,
  LoginConnected,
  LoginOffline,
} from "@/components/molecules/login/login-states";
import {
  type Broker,
  useBrokerDisconnect,
  useBrokerLoginUrl,
  useBrokerStatus,
  useBrokerToken,
} from "@/lib/queries/auth";

/** What the login box calls the broker and the code its redirect carries. */
const LOGIN_TEXT: Record<Broker, { name: string; code: string; connectedLabel: string }> = {
  fyers: { name: "Fyers", code: "auth_code", connectedLabel: "Connected" },
  zerodha: { name: "Zerodha", code: "request_token", connectedLabel: "Zerodha connected" },
};

/** The login flow's local state and handlers: start, paste the code, disconnect. */
function useLoginFlow(broker: Broker, name: string) {
  const loginUrl = useBrokerLoginUrl(broker);
  const token = useBrokerToken(broker);
  const disconnect = useBrokerDisconnect(broker);
  const [showModal, setShowModal] = useState(false);
  const [authCode, setAuthCode] = useState("");
  const [outcome, setOutcome] = useState<AuthOutcome>("idle");
  const [message, setMessage] = useState("");

  const fail = (text: string) => {
    setOutcome("error");
    setMessage(text);
  };

  const handleDisconnect = () => {
    if (!confirm(`Disconnect from ${name}? You will need to re-authenticate.`)) return;
    disconnect.mutate(undefined, {
      onSuccess: () => {
        setMessage("Disconnected successfully");
        setOutcome("success");
        setTimeout(() => setOutcome("idle"), 2000);
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
    setOutcome("idle");
    setMessage("");
    loginUrl.mutate(undefined, {
      onSuccess: openLoginPage,
      onError: (error) => fail(error.message),
    });
  };

  const handleSubmit = () => {
    if (!authCode) return;
    setOutcome("idle");
    token.mutate(authCode, {
      onSuccess: () => {
        setOutcome("success");
        setMessage("Connected successfully!");
        setTimeout(() => {
          setShowModal(false);
          setAuthCode("");
        }, 2000);
      },
      onError: (error) => fail(error.message || "Authentication failed"),
    });
  };

  return {
    loading: loginUrl.isPending || token.isPending || disconnect.isPending,
    showModal,
    closeModal: () => setShowModal(false),
    authCode,
    setAuthCode,
    outcome,
    message,
    handleDisconnect,
    handleLoginStart,
    handleSubmit,
  };
}

/** The header's broker login: connection state, and the login and disconnect flows. */
export function LoginButton({ broker = "fyers" }: Readonly<{ broker?: Broker }>) {
  const { name, code, connectedLabel } = LOGIN_TEXT[broker];
  const statusQuery = useBrokerStatus(broker);
  const flow = useLoginFlow(broker, name);
  const systemOffline = statusQuery.data?.offline ?? false;

  // Spinner on first load and on a manual retry from the offline badge, not on background refetches.
  if (statusQuery.isPending || (statusQuery.isFetching && systemOffline)) return <LoginChecking />;
  if (systemOffline) return <LoginOffline onRetry={() => void statusQuery.refetch()} />;
  if (statusQuery.data?.connected) {
    return (
      <LoginConnected
        name={name}
        label={connectedLabel}
        userId={statusQuery.data.userId}
        disabled={flow.loading}
        onDisconnect={flow.handleDisconnect}
      />
    );
  }
  return (
    <>
      <LoginConnect
        name={name}
        disabled={flow.loading}
        error={flow.outcome === "error" && !flow.showModal ? flow.message : ""}
        onConnect={flow.handleLoginStart}
      />
      {flow.showModal && (
        <AuthCodeDialog
          inputId={`${broker}-auth-code`}
          name={name}
          code={code}
          authCode={flow.authCode}
          loading={flow.loading}
          outcome={flow.outcome}
          message={flow.message}
          onChange={flow.setAuthCode}
          onSubmit={flow.handleSubmit}
          onClose={flow.closeModal}
        />
      )}
    </>
  );
}
