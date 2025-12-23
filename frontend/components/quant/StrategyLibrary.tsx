"use client";

import React, { useEffect, useState } from 'react';
import { GlassCard } from "@/components/ui/GlassCard";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { AlertCircle, Search, Edit2, Save, Activity } from 'lucide-react';
import { GlassSelect } from "@/components/ui/GlassSelect";

interface StrategyMetadata {
    strategy_id: string;
    display_name: string;
    description: string;
    regime_notes?: string;
    lifecycle_status: string;
    risk_profile: {
        regime?: string;
        risk_score?: number;
    }
}

import { apiClient } from "@/lib/api-client";

export function StrategyLibrary() {
    const [strategies, setStrategies] = useState<StrategyMetadata[]>([]);
    const [loading, setLoading] = useState(true);
    const [editingId, setEditingId] = useState<string | null>(null);
    const [editForm, setEditForm] = useState<{ regime_notes: string, lifecycle_status: string }>({ regime_notes: '', lifecycle_status: '' });

    useEffect(() => {
        fetchStrategies();
    }, []);

    const fetchStrategies = async () => {
        try {
            const response = await apiClient.get<StrategyMetadata[]>('/api/portfolio/strategies/available');
            if (response.data) {
                setStrategies(response.data);
            } else {
                console.error("Failed to fetch strategies", response.error);
            }
        } catch (error) {
            console.error("Failed to fetch strategies", error);
        } finally {
            setLoading(false);
        }
    };

    const handleEdit = (s: StrategyMetadata) => {
        setEditingId(s.strategy_id);
        setEditForm({
            regime_notes: s.regime_notes || '',
            lifecycle_status: s.lifecycle_status || 'RESEARCH'
        });
    };

    const handleSave = async (id: string) => {
        try {
            const response = await apiClient.patch(`/api/portfolio/strategies/${id}`, editForm);

            if (response.data || !response.error) {
                setEditingId(null);
                fetchStrategies();
            } else {
                alert("Update Failed: " + (response.error?.message || "Unknown error"));
            }
        } catch (err) {
            alert("Error saving strategy: " + (err instanceof Error ? err.message : 'Unknown error'));
        }
    };

    const getStatusColor = (status: string) => {
        switch (status) {
            case 'LIVE': return 'bg-green-500/10 text-green-500 hover:bg-green-500/20 border-green-500/20';
            case 'INCUBATION': return 'bg-yellow-500/10 text-yellow-500 hover:bg-yellow-500/20 border-yellow-500/20';
            case 'RETIRED': return 'bg-red-500/10 text-red-500 hover:bg-red-500/20 border-red-500/20';
            default: return 'bg-blue-500/10 text-blue-500 hover:bg-blue-500/20 border-blue-500/20';
        }
    };

    if (loading) return (
        <div className="flex flex-col items-center justify-center p-20 animate-pulse">
            <Activity className="w-10 h-10 text-cyan-500 mb-4" />
            <div className="text-sm font-bold text-cyan-500 tracking-widest uppercase">Loading Strategy Matrix...</div>
        </div>
    );

    return (
        <div className="space-y-6">
            {/* Header Toolbar */}
            <div className="flex items-center justify-between p-4 bg-black/40 border border-white/5 rounded-xl backdrop-blur-md">
                <div>
                    <h2 className="text-xl font-bold tracking-tight text-white flex items-center gap-3">
                        <Activity className="w-5 h-5 text-purple-500" />
                        Strategy Library
                    </h2>
                    <p className="text-xs text-gray-400 mt-1 font-mono">Manage algo lifecycle and risk parameters.</p>
                </div>
                <div className="flex gap-3">
                    <Button variant="outline" className="h-8 text-xs font-bold border-white/10 bg-white/5 hover:bg-white/10 text-gray-300">
                        <Search className="mr-2 h-3.5 w-3.5" /> Filter
                    </Button>
                    <Button className="h-8 text-xs font-bold bg-cyan-600 hover:bg-cyan-500 text-white shadow-lg shadow-cyan-900/20">
                        <Activity className="mr-2 h-3.5 w-3.5" /> New Strategy
                    </Button>
                </div>
            </div>

            {/* Strategy Grid */}
            <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-6">
                {strategies.map(s => (
                    <GlassCard key={s.strategy_id} className="group relative hover:border-cyan-500/30 transition-all duration-300 hover:shadow-[0_0_30px_rgba(8,145,178,0.1)]">
                        {/* Status Strip */}
                        <div className={`absolute left-0 top-0 bottom-0 w-1 ${s.lifecycle_status === 'LIVE' ? 'bg-green-500' :
                            s.lifecycle_status === 'INCUBATION' ? 'bg-yellow-500' : 'bg-blue-500'
                            }`} />

                        <div className="p-5 pl-7">
                            {/* Header */}
                            <div className="flex justify-between items-start mb-4">
                                <div>
                                    <div className="flex items-center gap-2 mb-2">
                                        <Badge className={`px-2 py-0.5 text-[10px] font-mono uppercase tracking-wider border ${getStatusColor(s.lifecycle_status)}`}>
                                            {s.lifecycle_status}
                                        </Badge>
                                        {s.risk_profile?.regime && (
                                            <Badge variant="outline" className="text-[10px] font-mono border-white/10 text-gray-400 bg-white/5">
                                                {s.risk_profile.regime}
                                            </Badge>
                                        )}
                                    </div>
                                    <h3 className="text-lg font-bold text-white tracking-tight group-hover:text-cyan-400 transition-colors">
                                        {s.display_name || s.strategy_id}
                                    </h3>
                                </div>
                                {editingId !== s.strategy_id && (
                                    <Button variant="ghost" size="icon" className="h-8 w-8 text-gray-500 hover:text-white hover:bg-white/10" onClick={() => handleEdit(s)}>
                                        <Edit2 className="h-4 w-4" />
                                    </Button>
                                )}
                            </div>

                            {/* Content */}
                            <div className="space-y-4">
                                <div>
                                    <h4 className="text-[10px] font-bold uppercase text-gray-500 mb-1.5 tracking-widest">Description</h4>
                                    <p className="text-xs text-gray-300 leading-relaxed font-medium bg-black/20 p-2 rounded border border-white/5">
                                        {s.description}
                                    </p>
                                </div>

                                {editingId === s.strategy_id ? (
                                    <div className="space-y-4 bg-white/5 p-4 rounded-lg border border-white/10 animate-in fade-in zoom-in-95 duration-200">
                                        <div>
                                            <h4 className="text-[10px] font-bold uppercase text-red-400 mb-1.5 tracking-widest flex items-center gap-2">
                                                <AlertCircle className="w-3 h-3" /> Forensic Notes
                                            </h4>
                                            <textarea
                                                value={editForm.regime_notes}
                                                onChange={(e) => setEditForm({ ...editForm, regime_notes: e.target.value })}
                                                placeholder="e.g. Fails in chopping sideways markets..."
                                                className="flex min-h-[80px] w-full rounded-md border border-white/10 bg-black/80 px-3 py-2 text-xs font-mono text-gray-300 ring-offset-background placeholder:text-gray-600 focus-visible:outline-none focus-visible:border-red-500/50 transition-colors resize-none"
                                            />
                                        </div>
                                        <div>
                                            <h4 className="text-[10px] font-bold uppercase text-blue-400 mb-1.5 tracking-widest">Lifecycle Status</h4>
                                            <GlassSelect
                                                value={editForm.lifecycle_status}
                                                onChange={(val) => setEditForm({ ...editForm, lifecycle_status: val })}
                                                options={[
                                                    { value: "RESEARCH", label: "RESEARCH" },
                                                    { value: "INCUBATION", label: "INCUBATION" },
                                                    { value: "LIVE", label: "LIVE" },
                                                    { value: "RETIRED", label: "RETIRED" },
                                                ]}
                                            />
                                        </div>
                                        <div className="flex justify-end gap-2 pt-2 border-t border-white/5">
                                            <Button size="sm" variant="ghost" className="h-7 text-xs hover:bg-white/10" onClick={() => setEditingId(null)}>Cancel</Button>
                                            <Button size="sm" className="h-7 text-xs bg-green-600 hover:bg-green-500 text-white" onClick={() => handleSave(s.strategy_id)}>
                                                <Save className="mr-1.5 h-3 w-3" /> Save Changes
                                            </Button>
                                        </div>
                                    </div>
                                ) : (
                                    <div>
                                        <h4 className="text-[10px] font-bold uppercase text-red-400/80 mb-1.5 flex items-center tracking-widest group-hover:text-red-400 transition-colors">
                                            <AlertCircle className="h-3 w-3 mr-1.5" /> Weakness / Failure Cases
                                        </h4>
                                        <div className="text-xs text-gray-400 font-mono bg-[#110000]/50 border border-red-900/20 p-2.5 rounded text-wrap break-words">
                                            {s.regime_notes ? `> ${s.regime_notes}` : <span className="opacity-50 italic">No forensic data available.</span>}
                                        </div>
                                    </div>
                                )}
                            </div>
                        </div>
                    </GlassCard>
                ))}
            </div>
        </div>
    );
}
