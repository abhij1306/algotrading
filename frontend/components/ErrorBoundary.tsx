"use client"

import { Component, ReactNode } from 'react'

interface Props {
    children: ReactNode
}

interface State {
    hasError: boolean
    error: Error | null
    errorInfo: any
}

export class ErrorBoundary extends Component<Props, State> {
    constructor(props: Props) {
        super(props)
        this.state = {
            hasError: false,
            error: null,
            errorInfo: null,
        }
    }

    static getDerivedStateFromError(error: Error): State {
        return {
            hasError: true,
            error,
            errorInfo: null,
        }
    }

    componentDidCatch(error: Error, errorInfo: any) {
        console.error('ErrorBoundary caught an error:', error, errorInfo)

        this.setState({
            error,
            errorInfo,
        })

        // Log to external service (e.g., Sentry) in production
        if (process.env.NODE_ENV === 'production') {
            // logErrorToService(error, errorInfo)
        }
    }

    render() {
        if (this.state.hasError) {
            return (
                <div className="min-h-screen bg-obsidian flex items-center justify-center p-6">
                    <div className="glass-strong rounded-xl p-8 max-w-2xl w-full">
                        <div className="flex items-center gap-3 mb-4">
                            <span className="text-4xl">⚠️</span>
                            <h1 className="text-2xl font-semibold text-text-primary">
                                Something went wrong
                            </h1>
                        </div>

                        <p className="text-text-secondary mb-6">
                            The application encountered an unexpected error. This has been logged and will be investigated.
                        </p>

                        {process.env.NODE_ENV === 'development' && this.state.error && (
                            <div className="space-y-4">
                                <div className="glass rounded-lg p-4">
                                    <h2 className="text-sm font-semibold text-text-primary mb-2">
                                        Error Details:
                                    </h2>
                                    <pre className="text-xs text-loss font-mono overflow-auto">
                                        {this.state.error.toString()}
                                    </pre>
                                </div>

                                {this.state.errorInfo && (
                                    <div className="glass rounded-lg p-4">
                                        <h2 className="text-sm font-semibold text-text-primary mb-2">
                                            Component Stack:
                                        </h2>
                                        <pre className="text-xs text-text-tertiary font-mono overflow-auto max-h-64">
                                            {this.state.errorInfo.componentStack}
                                        </pre>
                                    </div>
                                )}
                            </div>
                        )}

                        <div className="flex gap-3 mt-6">
                            <button
                                onClick={() => window.location.reload()}
                                className="px-4 py-2 bg-electric-blue hover:bg-electric-blue/80 
                         text-white rounded-lg transition-colors font-medium"
                            >
                                Reload Page
                            </button>
                            <button
                                onClick={() => this.setState({ hasError: false, error: null, errorInfo: null })}
                                className="px-4 py-2 glass hover:glass-hover 
                         text-text-primary rounded-lg transition-colors"
                            >
                                Try Again
                            </button>
                        </div>
                    </div>
                </div>
            )
        }

        return this.props.children
    }
}
