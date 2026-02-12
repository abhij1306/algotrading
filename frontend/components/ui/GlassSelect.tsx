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
                className={`flex items-center justify-between gap-3 px-3 py-1.5 bg-black/40 border border-white/10 rounded-lg text-white text-xs font-medium hover:border-cyan-500/50 transition-all ${className} ${isOpen ? 'border-cyan-500/50 ring-1 ring-cyan-500/20' : ''}`}
            >
                <span className={`truncate ${!selectedOption ? 'text-gray-500' : ''}`}>
                    {selectedOption ? selectedOption.label : placeholder}
                </span>
                <ChevronDown className={`w-3.5 h-3.5 text-gray-500 transition-transform ${isOpen ? 'rotate-180 text-cyan-500' : ''}`} />
            </button>

            {isOpen && (
                <Portal>
                    {/* Transparent Overlay to close */}
                    <div className="fixed inset-0 z-[9998]" onClick={() => setIsOpen(false)} />

                    <div
                        className="fixed z-[9999] bg-[#1a1d24] border border-white/10 rounded-lg shadow-2xl overflow-hidden glass-select-dropdown animate-in fade-in zoom-in-95 duration-100"
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
                                            ? 'bg-cyan-500/10 text-cyan-400'
                                            : 'text-gray-300 hover:bg-white/5 hover:text-white'
                                        }`}
                                >
                                    <span className="truncate">{option.label}</span>
                                    {option.value === value && <Check className="w-3 h-3 text-cyan-500" />}
                                </button>
                            ))}
                            {options.length === 0 && (
                                <div className="px-3 py-2 text-xs text-gray-500 italic text-center">No options</div>
                            )}
                        </div>
                    </div>
                </Portal>
            )}
        </>
    );
}
