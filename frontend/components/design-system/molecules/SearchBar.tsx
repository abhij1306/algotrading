'use client'

import * as React from 'react'
import { Input, IconButton } from '../atoms'
import { Search, X } from 'lucide-react'
import { cn } from '@/lib/utils'

export interface SearchBarProps {
    value: string
    onChange: (value: string) => void
    placeholder?: string
    onSearch?: (value: string) => void
    suggestions?: string[]
    className?: string
    autoFocus?: boolean
}

const SearchBar = React.forwardRef<HTMLInputElement, SearchBarProps>(
    (
        {
            value,
            onChange,
            placeholder = 'Search...',
            onSearch,
            suggestions = [],
            className,
            autoFocus = false,
        },
        ref
    ) => {
        const [isFocused, setIsFocused] = React.useState(false)
        const [showSuggestions, setShowSuggestions] = React.useState(false)

        const handleClear = () => {
            onChange('')
            setShowSuggestions(false)
        }

        const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
            if (e.key === 'Enter' && onSearch) {
                onSearch(value)
                setShowSuggestions(false)
            }
            if (e.key === 'Escape') {
                setShowSuggestions(false)
            }
        }

        const handleSuggestionClick = (suggestion: string) => {
            onChange(suggestion)
            setShowSuggestions(false)
            onSearch?.(suggestion)
        }

        const filteredSuggestions = suggestions.filter((s) =>
            s.toLowerCase().includes(value.toLowerCase())
        )

        return (
            <div className={cn('relative', className)}>
                {/* Search input with frosted glass */}
                <div
                    className={cn(
                        'relative transition-all duration-200',
                        isFocused && 'scale-[1.02]'
                    )}
                >
                    <Input
                        ref={ref}
                        value={value}
                        onChange={(e) => {
                            onChange(e.target.value)
                            setShowSuggestions(true)
                        }}
                        onFocus={() => {
                            setIsFocused(true)
                            setShowSuggestions(true)
                        }}
                        onBlur={() => {
                            setIsFocused(false)
                            // Delay to allow suggestion click
                            setTimeout(() => setShowSuggestions(false), 200)
                        }}
                        onKeyDown={handleKeyDown}
                        placeholder={placeholder}
                        icon={<Search className="w-4 h-4" />}
                        iconPosition="left"
                        variant="default"
                        className="pr-10"
                        autoFocus={autoFocus}
                    />

                    {/* Clear button */}
                    {value && (
                        <div className="absolute right-2 top-1/2 -translate-y-1/2">
                            <IconButton
                                variant="ghost"
                                size="sm"
                                icon={<X className="w-4 h-4" />}
                                onClick={handleClear}
                            />
                        </div>
                    )}
                </div>

                {/* Suggestions dropdown */}
                {showSuggestions && filteredSuggestions.length > 0 && (
                    <div className="absolute top-full left-0 right-0 mt-2 glass-strong rounded-lg border border-glass-border shadow-neomorph-lg overflow-hidden z-dropdown animate-fade-in">
                        <div className="max-h-64 overflow-y-auto">
                            {filteredSuggestions.map((suggestion, index) => (
                                <button
                                    key={index}
                                    onClick={() => handleSuggestionClick(suggestion)}
                                    className="w-full px-4 py-2.5 text-left text-sm text-text-primary hover:bg-white/5 transition-colors duration-150 first:rounded-t-lg last:rounded-b-lg"
                                >
                                    <div className="flex items-center gap-2">
                                        <Search className="w-3.5 h-3.5 text-text-tertiary" />
                                        <span>{suggestion}</span>
                                    </div>
                                </button>
                            ))}
                        </div>
                    </div>
                )}
            </div>
        )
    }
)

SearchBar.displayName = 'SearchBar'

export { SearchBar }
