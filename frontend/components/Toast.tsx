"use client";

import { ReactNode, createContext, useCallback, useContext, useState } from "react";

type ToastType = "success" | "error" | "warning" | "info";

interface Toast {
  id: string;
  type: ToastType;
  message: string;
  duration?: number;
}

interface ToastContextType {
  toasts: Toast[];
  addToast: (_type: ToastType, _message: string, _duration?: number) => void;
  removeToast: (_id: string) => void;
}

const ToastContext = createContext<ToastContextType | undefined>(undefined);

export function ToastProvider({ children }: Readonly<{ children: ReactNode }>) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const removeToast = useCallback((id: string) => {
    setToasts((prev) => prev.filter((toast) => toast.id !== id));
  }, []);

  const addToast = useCallback(
    (type: ToastType, message: string, duration = 3000) => {
      const id = Math.random().toString(36).substring(7);
      const toast: Toast = { id, type, message, duration };

      setToasts((prev) => [...prev, toast]);

      if (duration > 0) {
        setTimeout(() => {
          removeToast(id);
        }, duration);
      }
    },
    [removeToast]
  );

  return (
    <ToastContext.Provider value={{ toasts, addToast, removeToast }}>
      {children}
      <ToastContainer toasts={toasts} onRemove={removeToast} />
    </ToastContext.Provider>
  );
}

export function useToast() {
  const context = useContext(ToastContext);
  if (!context) {
    throw new Error("useToast must be used within ToastProvider");
  }
  return context;
}

function ToastContainer({
  toasts,
  onRemove,
}: Readonly<{
  toasts: Toast[];
  onRemove: (_id: string) => void;
}>) {
  if (toasts.length === 0) return null;

  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 max-w-sm">
      {toasts.map((toast) => (
        <ToastItem key={toast.id} toast={toast} onRemove={onRemove} />
      ))}
    </div>
  );
}

function ToastItem({ toast, onRemove }: Readonly<{ toast: Toast; onRemove: (_id: string) => void }>) {
  const icons = {
    success: "✅",
    error: "❌",
    warning: "⚠️",
    info: "ℹ️",
  };

  const colors = {
    success: "border-profit/20 bg-profit-bg",
    error: "border-loss/20 bg-loss-bg",
    warning: "border-warning/20 bg-warning-bg",
    info: "border-primary/20 bg-primary-light",
  };

  return (
      <div
        className={`glass-strong rounded-lg p-4 border ${colors[toast.type]}
                  animate-slide-in flex items-start gap-3 min-w-[300px]`}
      >
        <span className="text-xl">{icons[toast.type]}</span>
        <div className="flex-1">
          <p className="text-sm text-foreground">{toast.message}</p>
        </div>
        <button
          onClick={() => onRemove(toast.id)}
          className="text-foreground-muted hover:text-foreground transition-colors"
          aria-label="Dismiss notification"
        >
          ✕
        </button>
      </div>
    );
}

// Convenience hooks
export function useSuccessToast() {
  const { addToast } = useToast();
  return useCallback((message: string) => addToast("success", message), [addToast]);
}

export function useErrorToast() {
  const { addToast } = useToast();
  return useCallback((message: string) => addToast("error", message), [addToast]);
}
