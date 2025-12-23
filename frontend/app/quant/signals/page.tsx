'use client';

import { Activity, Radio } from 'lucide-react';
import { GlassCard } from '@/components/ui/GlassCard';

export default function SignalsPage() {
    return (
        <div className="h-full w-full p-6 text-gray-200 font-sans">
            <div className="h-full w-full flex flex-col items-center justify-center space-y-6">
                <GlassCard className="p-12 flex flex-col items-center text-center max-w-lg border-white/10 bg-white/5">
                    <div className="w-16 h-16 rounded-full bg-cyan-500/10 flex items-center justify-center mb-6 shadow-lg shadow-cyan-500/20 border border-cyan-500/20">
                        <Radio className="w-8 h-8 text-cyan-400 animate-pulse" />
                    </div>

                    <h2 className="text-2xl font-bold bg-gradient-to-r from-white to-gray-400 bg-clip-text text-transparent mb-3">
                        Live Signals
                    </h2>

                    <p className="text-sm text-gray-500 leading-relaxed mb-8">
                        The autonomous signal generation module is currently coming online.
                        Real-time trade opportunities and market anomalies will appear here.
                    </p>

                    <div className="flex gap-3">
                        <span className="px-3 py-1 rounded-full bg-yellow-500/10 text-yellow-500 text-[10px] font-bold uppercase tracking-wider border border-yellow-500/20">
                            System Standby
                        </span>
                        <span className="px-3 py-1 rounded-full bg-cyan-500/10 text-cyan-500 text-[10px] font-bold uppercase tracking-wider border border-cyan-500/20">
                            v2.0 Beta
                        </span>
                    </div>
                </GlassCard>
            </div>
        </div>
    );
}
