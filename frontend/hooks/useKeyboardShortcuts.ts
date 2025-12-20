"use client"

import { useEffect } from 'react'
import { useRouter } from 'next/navigation'

/**
 * Global keyboard shortcuts hook
 * Implements Raycast-style keyboard navigation
 */
export function useKeyboardShortcuts() {
    const router = useRouter()

    useEffect(() => {
        const handleKeyDown = (e: KeyboardEvent) => {
            // Ignore if user is typing in an input
            if (
                e.target instanceof HTMLInputElement ||
                e.target instanceof HTMLTextAreaElement ||
                e.target instanceof HTMLSelectElement
            ) {
                return
            }

            const isMac = navigator.platform.toUpperCase().indexOf('MAC') >= 0
            const modKey = isMac ? e.metaKey : e.ctrlKey

            // Cmd/Ctrl + B: Run Backtest
            if (e.key === 'b' && modKey) {
                e.preventDefault()
                router.push('/strategies')
            }

            // Cmd/Ctrl + P: Portfolio
            if (e.key === 'p' && modKey) {
                e.preventDefault()
                router.push('/')
            }

            // Cmd/Ctrl + S: Screener (override browser save)
            if (e.key === 's' && modKey) {
                e.preventDefault()
                router.push('/')
            }

            // Cmd/Ctrl + ,: Settings
            if (e.key === ',' && modKey) {
                e.preventDefault()
                // router.push('/settings')
                console.log('Settings shortcut pressed')
            }

            // ?: Help modal
            if (e.key === '?' && !modKey) {
                e.preventDefault()
                // TODO: Show keyboard shortcuts help modal
                console.log('Help shortcut pressed')
            }

            // Escape: Close modals/dialogs
            if (e.key === 'Escape') {
                // Let components handle this individually
            }
        }

        document.addEventListener('keydown', handleKeyDown)
        return () => document.removeEventListener('keydown', handleKeyDown)
    }, [router])
}

/**
 * Table keyboard navigation hook
 * For Stock Screener and other data tables
 */
export function useTableNavigation(
    items: any[],
    onSelect: (item: any) => void,
    enabled: boolean = true
) {
    useEffect(() => {
        if (!enabled || items.length === 0) return

        let selectedIndex = 0

        const handleKeyDown = (e: KeyboardEvent) => {
            // Ignore if user is typing
            if (
                e.target instanceof HTMLInputElement ||
                e.target instanceof HTMLTextAreaElement
            ) {
                return
            }

            // Arrow Up
            if (e.key === 'ArrowUp') {
                e.preventDefault()
                selectedIndex = Math.max(0, selectedIndex - 1)
                // Highlight row (emit event or use state)
                document.dispatchEvent(
                    new CustomEvent('table-nav', { detail: { index: selectedIndex } })
                )
            }

            // Arrow Down
            if (e.key === 'ArrowDown') {
                e.preventDefault()
                selectedIndex = Math.min(items.length - 1, selectedIndex + 1)
                document.dispatchEvent(
                    new CustomEvent('table-nav', { detail: { index: selectedIndex } })
                )
            }

            // Enter: Select item
            if (e.key === 'Enter') {
                e.preventDefault()
                if (items[selectedIndex]) {
                    onSelect(items[selectedIndex])
                }
            }

            // Space: Toggle selection (for multi-select)
            if (e.key === ' ') {
                e.preventDefault()
                document.dispatchEvent(
                    new CustomEvent('table-toggle', { detail: { index: selectedIndex } })
                )
            }
        }

        document.addEventListener('keydown', handleKeyDown)
        return () => document.removeEventListener('keydown', handleKeyDown)
    }, [items, onSelect, enabled])
}
