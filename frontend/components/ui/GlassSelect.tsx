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
    onChange: (value: any) => void;
    placeholder?: string;
    className?: string;
}

export function GlassSelect({ options, value, onChange, placeholder = 'Select...', className = '' }: GlassSelectProps) {
    const [isOpen, setIsOpen] = useState(false);
    const triggerRef = useRef<HTMLButtonElement>(null);
    const [coords, setCoords] = useState({ top: 0, left: 0, width: 0 });

    const selectedOption = options.find(o => o.value === value);

    const toggleOpen = () => {
        if (triggerRef.current) {
            const rect = triggerRef.current.getBoundingClientRect();
            setCoords({
                top: rect.bottom + window.scrollY + 8,
                left: rect.left + window.scrollX,
                width: rect.width
            });
        }
        setIsOpen(!isOpen);
    };

    // Close on click outside
    useEffect(() => {
        const handleClickOutside = (event: MouseEvent) => {
            if (triggerRef.current && !triggerRef.current.contains(event.target as Node)) {
                // We don't check the dropdown because it's in a portal, 
                // but usually clicking outside the portal overlay (if exists) or just document click works.
                // For a portal implementation without overlay, we might need a ref for the dropdown content too.
                // But simplified: checking if click is NOT on the trigger. 
                // If it's in the dropdown, the dropdown items will handle closing.
                // However, clicking "elsewhere" should close it.
                // Since the dropdown is in a portal, looking for .glass-select-dropdown might work,
                // or simpler: just use a transparent overlay in the portal.
            }
        };

        // Better Portal approach: The portal usually renders at document root.
        // We can just add a full-screen transparent click handler in the Portal when open.
    }, []);

    return (
        <>
            <button
                ref={triggerRef}
                onClick={toggleOpen}
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
                            top: coords.top,
                            left: coords.left,
                            width: coords.width,
                            maxHeight: '300px'
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
