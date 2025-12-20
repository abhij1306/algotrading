"use client"

import { createContext, useContext, useState, useCallback, ReactNode } from 'react'

type ToastType = 'success' | 'error' | 'warning' | 'info'

interface Toast {
    id: string
    type: ToastType
    message: string
    duration?: number
}

interface ToastContextType {
    toasts: Toast[]
    addToast: (type: ToastType, message: string, duration?: number) => void
    removeToast: (id: string) => void
}

const ToastContext = createContext<ToastContextType | undefined>(undefined)

export function ToastProvider({ children }: { children: ReactNode }) {
    const [toasts, setToasts] = useState<Toast[]>([])

    const addToast = useCallback((type: ToastType, message: string, duration = 3000) => {
        const id = Math.random().toString(36).substring(7)
        const toast: Toast = { id, type, message, duration }

        setToasts((prev) => [...prev, toast])

        if (duration > 0) {
            setTimeout(() => {
                removeToast(id)
            }, duration)
        }
    }, [])

    const removeToast = useCallback((id: string) => {
        setToasts((prev) => prev.filter((toast) => toast.id !== id))
    }, [])

    return (
        <ToastContext.Provider value={{ toasts, addToast, removeToast }}>
            {children}
            <ToastContainer toasts={toasts} onRemove={removeToast} />
        </ToastContext.Provider>
    )
}

export function useToast() {
    const context = useContext(ToastContext)
    if (!context) {
        throw new Error('useToast must be used within ToastProvider')
    }
    return context
}

function ToastContainer({
    toasts,
    onRemove,
}: {
    toasts: Toast[]
    onRemove: (id: string) => void
}) {
    if (toasts.length === 0) return null

    return (
        <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 max-w-sm">
            {toasts.map((toast) => (
                <ToastItem key={toast.id} toast={toast} onRemove={onRemove} />
            ))}
        </div>
    )
}

function ToastItem({ toast, onRemove }: { toast: Toast; onRemove: (id: string) => void }) {
    const icons = {
        success: '✅',
        error: '❌',
        warning: '⚠️',
        info: 'ℹ️',
    }

    const colors = {
        success: 'border-green-500/20 bg-green-500/10',
        error: 'border-red-500/20 bg-red-500/10',
        warning: 'border-yellow-500/20 bg-yellow-500/10',
        info: 'border-blue-500/20 bg-blue-500/10',
    }

    return (
        <div
            className={`glass-strong rounded-lg p-4 border ${colors[toast.type]} 
                  animate-slide-in flex items-start gap-3 min-w-[300px]`}
        >
            <span className="text-xl">{icons[toast.type]}</span>
            <div className="flex-1">
                <p className="text-sm text-white">{toast.message}</p>
            </div>
            <button
                onClick={() => onRemove(toast.id)}
                className="text-gray-400 hover:text-white transition-colors"
            >
                ✕
            </button>
        </div>
    )
}

// Convenience hooks
export function useSuccessToast() {
    const { addToast } = useToast()
    return useCallback((message: string) => addToast('success', message), [addToast])
}

export function useErrorToast() {
    const { addToast } = useToast()
    return useCallback((message: string) => addToast('error', message), [addToast])
}
