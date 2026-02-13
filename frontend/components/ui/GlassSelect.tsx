'use client';

import React, { useState, useRef, useEffect } from 'react';
import { ChevronDown, Check } from 'lucide-react';
import Portal from '@/components/ui/Portal';

interface Option {
    value: string | number;
    label: string;
}

interface GlassSelectProps {
    options: Option[];
    value: string | number | null;
    onChange: (value: string | number) => void;
    placeholder?: string;
    className?: string;
    position?: 'down' | 'up';
}

export function GlassSelect({ options, value, onChange, placeholder = 'Select...', className = '', position = 'down' }: GlassSelectProps) {
    const [isOpen, setIsOpen] = useState(false);
    const triggerRef = useRef<HTMLButtonElement>(null);
    const [coords, setCoords] = useState({ top: 0, left: 0, width: 0, positionAnchor: 'bottom' as 'top' | 'bottom' });

    const selectedOption = options.find(o => o.value === value);

    useEffect(() => {
        if (isOpen && triggerRef.current) {
            const rect = triggerRef.current.getBoundingClientRect();
            if (position === 'up') {
                setCoords({
                    top: rect.top,
                    left: rect.left,
                    width: rect.width,
                    positionAnchor: 'top'
                });
            } else {
                setCoords({
                    top: rect.bottom + 8,
                    left: rect.left,
                    width: rect.width,
                    positionAnchor: 'bottom'
                });
            }
        }

        const handleResize = () => {
            if (isOpen && triggerRef.current) {
                const rect = triggerRef.current.getBoundingClientRect();
                if (position === 'up') {
                    setCoords({
                        top: rect.top,
                        left: rect.left,
                        width: rect.width,
                        positionAnchor: 'top'
                    });
                } else {
                    setCoords({
                        top: rect.bottom + 8,
                        left: rect.left,
                        width: rect.width,
                        positionAnchor: 'bottom'
                    });
                }
            }
        };

        window.addEventListener('resize', handleResize);
        window.addEventListener('scroll', handleResize, true);

        return () => {
            window.removeEventListener('resize', handleResize);
            window.removeEventListener('scroll', handleResize, true);
        };
    }, [isOpen, position]);

    return (
        <>
            <button
                ref={triggerRef}
                onClick={() => setIsOpen(!isOpen)}
                className={`flex items-center justify-between gap-3 px-3 py-1.5 bg-[var(--color-surface)] border border-[var(--border-default)] rounded-lg text-[var(--text-primary)] text-xs font-medium hover:border-[var(--color-primary)] transition-all ${className} ${isOpen ? 'border-[var(--color-primary)] ring-1 ring-[var(--color-primary-bg)]' : ''}`}
            >
                <span className={`truncate ${!selectedOption ? 'text-[var(--text-muted)]' : ''}`}>
                    {selectedOption ? selectedOption.label : placeholder}
                </span>
                <ChevronDown className={`w-3.5 h-3.5 text-[var(--text-muted)] transition-transform ${isOpen ? 'rotate-180 text-[var(--color-primary)]' : ''}`} />
            </button>

            {isOpen && (
                <Portal>
                    {/* Transparent Overlay to close */}
                    <div className="fixed inset-0 z-[9998]" onClick={() => setIsOpen(false)} />

                    <div
                        className="fixed z-[9999] bg-[var(--color-elevated)] border border-[var(--border-default)] rounded-lg shadow-2xl overflow-hidden glass-select-dropdown animate-in fade-in zoom-in-95 duration-100"
                        style={{
                            top: `${coords.top}px`,
                            left: `${coords.left}px`,
                            width: `${coords.width}px`,
                            maxHeight: '300px',
                            transform: coords.positionAnchor === 'top' ? 'translateY(-100%) translateY(-8px)' : undefined
                        }}
                    >
                        <div className="max-h-[300px] overflow-y-auto custom-scrollbar p-1">
                            {options.map((option) => (
                                <button
                                    key={option.value}
                                    onClick={() => {
                                        onChange(option.value);
                                        setIsOpen(false);
                                    }}
                                    className={`w-full text-left px-3 py-2 rounded-md text-xs font-medium flex items-center justify-between group transition-colors ${option.value === value
                                            ? 'bg-[var(--color-primary-bg)] text-[var(--color-primary)]'
                                            : 'text-[var(--text-secondary)] hover:bg-[var(--glass-highlight)] hover:text-[var(--text-primary)]'
                                        }`}
                                >
                                    <span className="truncate">{option.label}</span>
                                    {option.value === value && <Check className="w-3 h-3 text-[var(--color-primary)]" />}
                                </button>
                            ))}
                            {options.length === 0 && (
                                <div className="px-3 py-2 text-xs text-[var(--text-muted)] italic text-center">No options</div>
                            )}
                        </div>
                    </div>
                </Portal>
            )}
        </>
    );
}
