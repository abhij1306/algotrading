import { useState } from "react";
import { AuthCodeDialog, type AuthOutcome } from "@/components/molecules/login/auth-code-dialog";
import {
  LoginChecking,
  LoginConnect,
  LoginConnected,
  LoginOffline,
  LoginUnreadable,
} from "@/components/molecules/login/login-states";
import {
  type Broker,
  useBrokerDisconnect,
  useBrokerLoginUrl,
  useBrokerStatus,
  useBrokerToken,
} from "@/lib/queries/auth";

/** What the login box calls the broker and the code its redirect carries. */
const LOGIN_TEXT: Record<Broker, { name: string; code: string }> = {
  fyers: { name: "Fyers", code: "auth_code" },
  zerodha: { name: "Zerodha", code: "request_token" },
};

const POPUP_FEATURES = "width=800,height=600";

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

  // The window opens inside the click, where popup blockers allow it, and is sent to the
  // broker's login page once the backend returns the URL.
  const handleLoginStart = () => {
    setOutcome("idle");
    setMessage("");
    const popup = globalThis.open("", "_blank", POPUP_FEATURES);
    if (!popup) {
      fail("Popup blocked; allow popups for this site to log in.");
      return;
    }
    popup.opener = null;
    loginUrl.mutate(undefined, {
      onSuccess: (url) => {
        popup.location.href = url;
        setShowModal(true);
      },
      onError: (error) => {
        popup.close();
        fail(error.message);
      },
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

/** The broker's header state while the login modal is closed. */
function LoginStatus({
  broker,
  name,
  flow,
}: Readonly<{ broker: Broker; name: string; flow: ReturnType<typeof useLoginFlow> }>) {
  const statusQuery = useBrokerStatus(broker);
  const [retrying, setRetrying] = useState(false);
  const status = statusQuery.data;
  const error = flow.outcome === "error" && !flow.showModal ? flow.message : "";

  if (status === undefined || retrying) return <LoginChecking name={name} />;
  switch (status.kind) {
    case "offline": {
      const retry = () => {
        setRetrying(true);
        void statusQuery.refetch().finally(() => setRetrying(false));
      };
      return <LoginOffline onRetry={retry} />;
    }
    case "unreadable":
      return <LoginUnreadable name={name} error={status.error} />;
    case "connected":
      return (
        <LoginConnected
          name={name}
          userId={status.userId}
          disabled={flow.loading}
          error={error}
          onDisconnect={flow.handleDisconnect}
        />
      );
    case "disconnected":
      return (
        <LoginConnect
          name={name}
          disabled={flow.loading}
          error={error}
          onConnect={flow.handleLoginStart}
        />
      );
  }
}

/** The header's broker login: connection state, and the login and disconnect flows. */
export function LoginButton({ broker = "fyers" }: Readonly<{ broker?: Broker }>) {
  const { name, code } = LOGIN_TEXT[broker];
  const flow = useLoginFlow(broker, name);
  return (
    <>
      <LoginStatus broker={broker} name={name} flow={flow} />
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
