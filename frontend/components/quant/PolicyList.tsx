
"use client";

import React, { useEffect, useState } from 'react';
import { GlassCard } from "@/components/ui/GlassCard";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Shield, Plus, Edit, Trash2 } from "lucide-react";

export default function PolicyList() {
    const [policies, setPolicies] = useState<any[]>([]);
    const [isEditing, setIsEditing] = useState(false);

    // Dynamic import to avoid SSR issues if any, or just import top level
    const PolicyEditor = require('./PolicyEditor').default;

    useEffect(() => {
        // Fetch policies
        fetch('http://localhost:9000/api/portfolio/strategies/policy')
            .then(res => res.json())
            .then(data => setPolicies(data))
            .catch(err => console.error("Failed to fetch policies", err));
    }, []);

    if (isEditing) {
        return (
            <div className="w-full animate-in fade-in slide-in-from-bottom-4 duration-500">
                <Button
                    variant="ghost"
                    className="mb-6 hover:bg-white/5 text-gray-400 hover:text-white"
                    onClick={() => setIsEditing(false)}
                >
                    &larr; Back to Policies
                </Button>
                <PolicyEditor
                    onCancel={() => setIsEditing(false)}
                    onSave={(policy: any) => {
                        console.log("Saving policy:", policy);
                        // Save to API
                        fetch('http://localhost:9000/api/portfolio/strategies/policy', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(policy)
                        })
                            .then(res => res.json())
                            .then(saved => {
                                setPolicies([...policies, saved]);
                                setIsEditing(false);
                            })
                            .catch(err => console.error("Save failed", err));
                    }}
                />
            </div>
        )
    }

    return (
        <GlassCard className="w-full relative overflow-hidden">
            {/* Header Section */}
            <div className="p-6 border-b border-white/10 flex flex-row items-center justify-between bg-black/20">
                <div>
                    <h2 className="text-lg font-bold text-white flex items-center gap-3">
                        <div className="p-2 rounded bg-blue-500/20 text-blue-400">
                            <Shield className="h-5 w-5" />
                        </div>
                        Risk Governance Policies
                    </h2>
                    <p className="text-xs text-gray-400 mt-1 ml-12">
                        Define strict risk limits and allocation rules for your portfolios.
                    </p>
                </div>
                <Button
                    onClick={() => setIsEditing(true)}
                    className="bg-blue-600 hover:bg-blue-500 text-white shadow-lg shadow-blue-900/20 border border-blue-500/50"
                >
                    <Plus className="mr-2 h-4 w-4" />
                    New Policy
                </Button>
            </div>

            {/* Table Section */}
            <div className="p-0">
                <div className="w-full overflow-auto">
                    <table className="w-full text-sm text-left">
                        <thead className="text-xs uppercase bg-white/5 text-gray-400 font-medium">
                            <tr>
                                <th className="px-6 py-4">Policy ID</th>
                                <th className="px-6 py-4">Cash Reserve</th>
                                <th className="px-6 py-4">Max Exposure</th>
                                <th className="px-6 py-4">Daily Stop Loss</th>
                                <th className="px-6 py-4">Max Allocation</th>
                                <th className="px-6 py-4">Sensitivity</th>
                                <th className="px-6 py-4 text-right">Actions</th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-white/5">
                            {policies.length === 0 ? (
                                <tr>
                                    <td colSpan={7} className="px-6 py-12 text-center text-gray-500">
                                        No policies found. Create one to get started.
                                    </td>
                                </tr>
                            ) : (
                                policies.map((policy) => (
                                    <tr key={policy.id} className="hover:bg-white/5 transition-colors group">
                                        <td className="px-6 py-4 font-mono text-xs text-gray-400 group-hover:text-white transition-colors">
                                            {policy.id.substring(0, 8)}...
                                        </td>
                                        <td className="px-6 py-4 font-mono text-cyan-400">
                                            {policy.cash_reserve_percent}%
                                        </td>
                                        <td className="px-6 py-4 font-mono">
                                            {policy.max_equity_exposure_percent}%
                                        </td>
                                        <td className="px-6 py-4 font-mono text-red-400 font-medium">
                                            {policy.daily_stop_loss_percent}%
                                        </td>
                                        <td className="px-6 py-4 font-mono">
                                            {policy.max_strategy_allocation_percent}%
                                        </td>
                                        <td className="px-6 py-4">
                                            <Badge variant="secondary" className="bg-white/10 text-gray-300 border-white/5 hover:bg-white/20">
                                                {policy.allocation_sensitivity}
                                            </Badge>
                                        </td>
                                        <td className="px-6 py-4 text-right">
                                            <Button variant="ghost" size="sm" className="h-8 w-8 hover:bg-white/10 hover:text-white">
                                                <Edit className="h-4 w-4" />
                                            </Button>
                                        </td>
                                    </tr>
                                ))
                            )}
                        </tbody>
                    </table>
                </div>
            </div>
        </GlassCard>
    );
}
