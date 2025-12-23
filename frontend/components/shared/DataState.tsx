/**
 * Reusable UI Components for Error, Loading, and Empty States
 */
import React from 'react';
import { AlertCircle, Loader2, Inbox } from 'lucide-react';

// ============================================
// Loading State Component
// ============================================

interface LoadingStateProps {
    message?: string;
}

export function LoadingState({ message = 'LOADING...' }: LoadingStateProps) {
    return (
        <div className="flex h-full min-h-[400px] flex-col items-center justify-center space-y-4">
            <Loader2 className="h-12 w-12 animate-spin text-cyan-500" />
            <p className="text-sm font-mono text-cyan-400 uppercase tracking-wider">{message}</p>
        </div>
    );
}

// ============================================
// Error State Component
// ============================================

interface ErrorStateProps {
    title?: string;
    message: string;
    onRetry?: () => void;
    retryLabel?: string;
}

export function ErrorState({
    title = 'ERROR',
    message,
    onRetry,
    retryLabel = 'TRY AGAIN',
}: ErrorStateProps) {
    return (
        <div className="flex h-full min-h-[400px] flex-col items-center justify-center space-y-4 p-8">
            <div className="rounded-lg bg-red-500/10 border border-red-500/30 p-6 max-w-md text-center">
                <AlertCircle className="h-12 w-12 text-red-400 mx-auto mb-4" />
                <h3 className="text-lg font-mono font-bold text-red-400 mb-2">{title}</h3>
                <p className="text-sm text-red-300/80 mb-4">{message}</p>
                {onRetry && (
                    <button
                        onClick={onRetry}
                        className="px-6 py-2 bg-red-500/20 hover:bg-red-500/30 border border-red-500/50 rounded text-red-300 font-mono text-sm uppercase tracking-wider transition-colors"
                    >
                        {retryLabel}
                    </button>
                )}
            </div>
        </div>
    );
}

// ============================================
// Empty State Component
// ============================================

interface EmptyStateProps {
    icon?: React.ReactNode;
    title: string;
    description?: string;
    action?: React.ReactNode;
}

export function EmptyState({ icon, title, description, action }: EmptyStateProps) {
    return (
        <div className="flex h-full min-h-[400px] flex-col items-center justify-center space-y-4 p-8">
            <div className="text-center max-w-md">
                <div className="mb-4 text-gray-500">
                    {icon || <Inbox className="h-16 w-16 mx-auto" />}
                </div>
                <h3 className="text-xl font-mono font-bold text-gray-300 mb-2">{title}</h3>
                {description && <p className="text-sm text-gray-400 mb-6">{description}</p>}
                {action && <div className="mt-6">{action}</div>}
            </div>
        </div>
    );
}

// ============================================
// Combined Data State Component
// ============================================

interface DataStateProps<T> {
    loading: boolean;
    error?: string;
    data?: T;
    isEmpty?: boolean;
    loadingMessage?: string;
    emptyTitle?: string;
    emptyDescription?: string;
    emptyAction?: React.ReactNode;
    onRetry?: () => void;
    children: (data: T) => React.ReactNode;
}

export function DataState<T>({
    loading,
    error,
    data,
    isEmpty,
    loadingMessage,
    emptyTitle = 'NO DATA',
    emptyDescription,
    emptyAction,
    onRetry,
    children,
}: DataStateProps<T>) {
    if (loading) {
        return <LoadingState message={loadingMessage} />;
    }

    if (error) {
        return <ErrorState message={error} onRetry={onRetry} />;
    }

    if (isEmpty || !data) {
        return (
            <EmptyState title={emptyTitle} description={emptyDescription} action={emptyAction} />
        );
    }

    return <>{children(data)}</>;
}
