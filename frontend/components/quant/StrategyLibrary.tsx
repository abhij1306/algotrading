"use client";

import React, { useEffect, useState } from 'react';
import { GlassCard } from "@/components/ui/GlassCard";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { AlertCircle, Search, Edit2, Save, Activity, Clock, Zap, Target, TrendingUp, AlertTriangle } from 'lucide-react';
import { GlassSelect } from "@/components/ui/GlassSelect";

interface StrategyContract {
    strategy_id: string;
    allowed_universes: string[];
    timeframe: string;
    holding_period: string;
    regime: string;
    description: string;
    when_loses: string;
    lifecycle_status?: string; // Added from backend
}

export function StrategyLibrary() {
    const [strategies, setStrategies] = useState<StrategyContract[]>([]);
    const [loading, setLoading] = useState(true);
    const [searchTerm, setSearchTerm] = useState("");

    // Edit state
    const [editingId, setEditingId] = useState<string | null>(null);
    const [editForm, setEditForm] = useState<{ lifecycle_status: string }>({ lifecycle_status: '' });

    useEffect(() => {
        fetchStrategies();
    }, []);

    const fetchStrategies = async () => {
        try {
            // Updated endpoint
            const response = await fetch('http://localhost:9000/api/portfolio/strategy-contracts');
            const data = await response.json();
            if (data.contracts) {
                setStrategies(data.contracts);
            }
        } catch (error) {
            console.error("Failed to fetch strategies", error);
        } finally {
            setLoading(false);
        }
    };

    const handleEdit = (s: StrategyContract) => {
        setEditingId(s.strategy_id);
        setEditForm({
            lifecycle_status: s.lifecycle_status || 'RESEARCH'
        });
    };

    const handleSave = async (id: string) => {
        try {
            // We need a specific endpoint to update lifecycle. 
            // Assuming /api/portfolio/strategies/lifecycle exists or we use the patch one.
            // Based on portfolio_live.py, we have: @router.post("/strategies/lifecycle")

            const response = await fetch(`http://localhost:9000/api/portfolio/strategies/lifecycle?strategy_id=${id}&new_state=${editForm.lifecycle_status}`, {
                method: 'POST'
            });

            if (response.ok) {
                setEditingId(null);
                fetchStrategies();
            } else {
                alert("Update Failed");
            }
        } catch (err) {
            alert("Error saving strategy");
        }
    };

    const getStatusColor = (status: string) => {
        switch (status) {
            case 'LIVE': return 'bg-emerald-500/10 text-emerald-500 border-emerald-500/20';
            case 'INCUBATION': return 'bg-amber-500/10 text-amber-500 border-amber-500/20';
            case 'RETIRED': return 'bg-red-500/10 text-red-500 border-red-500/20';
            default: return 'bg-blue-500/10 text-blue-500 border-blue-500/20';
        }
    };

    const filteredStrategies = strategies.filter(s =>
        s.strategy_id.toLowerCase().includes(searchTerm.toLowerCase()) ||
        s.regime.toLowerCase().includes(searchTerm.toLowerCase())
    );

    if (loading) return (
        <div className="flex flex-col items-center justify-center p-20 animate-pulse">
            <Activity className="w-10 h-10 text-cyan-500 mb-4" />
            <div className="text-sm font-bold text-cyan-500 tracking-widest uppercase">Loading Matrix...</div>
        </div>
    );

    return (
        <div className="max-w-[1920px] mx-auto space-y-8">
            {/* Header Toolbar */}
            <div className="flex items-center justify-between p-6 bg-[#0A0A0A] border border-white/5 rounded-xl">
                <div>
                    <h2 className="text-2xl font-bold tracking-tight text-white flex items-center gap-3">
                        <Zap className="w-6 h-6 text-cyan-400" />
                        Strategy Matrix
                    </h2>
                    <p className="text-sm text-gray-400 mt-1 font-mono">Manage institutional algorithm lifecycle.</p>
                </div>
                <div className="flex gap-3">
                    <div className="relative">
                        <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
                        <input
                            type="text"
                            placeholder="Filter strategies..."
                            className="bg-white/5 border border-white/10 rounded-lg pl-9 pr-4 py-2 text-sm text-white focus:outline-none focus:border-cyan-500/50 w-64 transition-all"
                            value={searchTerm}
                            onChange={(e) => setSearchTerm(e.target.value)}
                        />
                    </div>
                    {/* <Button className="bg-cyan-600 hover:bg-cyan-500 text-white font-bold">
                        <Activity className="mr-2 h-4 w-4" /> New Strategy
                    </Button> */}
                </div>
            </div>

            {/* Strategy Grid */}
            <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-6">
                {filteredStrategies.map(s => (
                    <div key={s.strategy_id} className="group relative bg-[#050505] rounded-xl border border-white/5 hover:border-cyan-500/30 transition-all duration-300 hover:shadow-2xl hover:shadow-cyan-900/10 overflow-hidden flex flex-col">

                        {/* Status Strip */}
                        <div className={`absolute left-0 top-0 bottom-0 w-1 ${s.lifecycle_status === 'LIVE' ? 'bg-emerald-500' :
                            s.lifecycle_status === 'INCUBATION' ? 'bg-amber-500' : 'bg-blue-500'
                            }`} />

                        <div className="p-6 pl-8 flex-1 flex flex-col">
                            {/* Header */}
                            <div className="flex justify-between items-start mb-6">
                                <div>
                                    <div className="flex items-center gap-2 mb-3">
                                        <span className={`px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider border ${getStatusColor(s.lifecycle_status || 'RESEARCH')}`}>
                                            {s.lifecycle_status || 'RESEARCH'}
                                        </span>
                                        <span className="px-2 py-0.5 rounded text-[10px] font-mono border bg-white/5 border-white/10 text-gray-400">
                                            {s.regime}
                                        </span>
                                    </div>
                                    <h3 className="text-lg font-bold text-white tracking-tight group-hover:text-cyan-400 transition-colors">
                                        {s.strategy_id.replace(/_/g, ' ')}
                                    </h3>
                                </div>
                                {editingId !== s.strategy_id && (
                                    <button
                                        className="p-2 text-gray-600 hover:text-white hover:bg-white/10 rounded-lg transition-colors"
                                        onClick={() => handleEdit(s)}
                                    >
                                        <Edit2 className="h-4 w-4" />
                                    </button>
                                )}
                            </div>

                            {/* Info Rows */}
                            <div className="grid grid-cols-2 gap-4 mb-6 text-xs text-gray-400">
                                <div className="flex items-center gap-2">
                                    <Clock className="w-3.5 h-3.5 text-gray-600" />
                                    <span className="font-mono">{s.timeframe}</span>
                                </div>
                                <div className="flex items-center gap-2">
                                    <Target className="w-3.5 h-3.5 text-gray-600" />
                                    <span className="font-mono">{s.holding_period}</span>
                                </div>
                            </div>

                            {/* Description */}
                            <div className="bg-white/[0.02] border border-white/5 rounded-lg p-4 mb-4 flex-1">
                                <h4 className="text-[10px] font-bold uppercase text-gray-600 mb-2">Thesis</h4>
                                <p className="text-sm text-gray-300 leading-relaxed font-medium">
                                    {s.description}
                                </p>
                            </div>

                            {/* Edit Mode vs View Mode */}
                            {editingId === s.strategy_id ? (
                                <div className="mt-auto pt-4 border-t border-white/5 animate-in slide-in-from-bottom-2">
                                    <h4 className="text-[10px] font-bold uppercase text-cyan-500 mb-2">Update Status</h4>
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
                                    <div className="flex justify-end gap-2 mt-4">
                                        <Button size="sm" variant="ghost" className="h-8 text-xs hover:bg-white/10 hover:text-white" onClick={() => setEditingId(null)}>Cancel</Button>
                                        <Button size="sm" className="h-8 text-xs bg-emerald-600 hover:bg-emerald-500 text-white" onClick={() => handleSave(s.strategy_id)}>
                                            <Save className="mr-1.5 h-3 w-3" /> Save
                                        </Button>
                                    </div>
                                </div>
                            ) : (
                                <div className="mt-auto">
                                    <div className="flex items-start gap-2 pt-4 border-t border-white/5">
                                        <AlertTriangle className="w-4 h-4 text-amber-500/70 mt-0.5 flex-shrink-0" />
                                        <div>
                                            <h4 className="text-[10px] font-bold uppercase text-amber-500/70 mb-0.5">Risk Scenario</h4>
                                            <p className="text-xs text-gray-500 italic">
                                                {s.when_loses}
                                            </p>
                                        </div>
                                    </div>
                                </div>
                            )}
                        </div>
                    </div>
                ))}
            </div>
        </div>
    );
}
