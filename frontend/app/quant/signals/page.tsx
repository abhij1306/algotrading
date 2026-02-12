'use client';

import { Radio } from 'lucide-react';
import { Button } from '@/components/ui/button';

export default function SignalsPage() {
    return (
        <div className="h-full w-full p-6 text-[var(--text-primary)]">
            <div className="h-full w-full flex flex-col items-center justify-center space-y-6">
                <div className="card p-12 flex flex-col items-center text-center max-w-lg">
                    <div className="w-16 h-16 rounded-full bg-[var(--color-primary-bg)] flex items-center justify-center mb-6 border border-[var(--color-primary)]/20">
                        <Radio className="w-8 h-8 text-[var(--color-primary)] animate-pulse" />
                    </div>

                    <h2 className="text-2xl font-bold text-[var(--text-primary)] mb-3">
                        Live Signals
                    </h2>

                    <p className="text-sm text-[var(--text-muted)] leading-relaxed mb-8">
                        The autonomous signal generation module is currently coming online.
                        Real-time trade opportunities and market anomalies will appear here.
                    </p>

                    <div className="flex gap-3">
                        <span className="px-3 py-1 rounded-full bg-[var(--color-loss-bg)] text-[var(--color-loss)] text-[10px] font-bold uppercase tracking-wider border border-[var(--color-loss)]/20">
                            System Standby
                        </span>
                        <span className="px-3 py-1 rounded-full bg-[var(--color-primary-bg)] text-[var(--color-primary)] text-[10px] font-bold uppercase tracking-wider border border-[var(--color-primary)]/20">
                            v2.0 Beta
                        </span>
                    </div>
                </div>
            </div>
        </div>
    );
}
