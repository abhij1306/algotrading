
"use client";

import React, { useState } from 'react';
import { GlassCard } from "@/components/ui/GlassCard";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Slider } from "@/components/ui/slider";
import { Label } from "@/components/ui/label";
import { Save, Scale, TrendingUp, Network, Shield, AlertTriangle } from "lucide-react";
import { cn } from "@/lib/utils";

interface PolicyEditorProps {
    onSave: (policy: any) => void;
    initialData?: any;
    onCancel: () => void;
}

const ALLOCATION_METHODS = [
    {
        id: "equal_trust",
        title: "Equal Trust",
        description: "Distributes capital evenly across all active strategies.",
        icon: Scale,
        recommended: false
    },
    {
        id: "risk_normalized",
        title: "Risk Normalized",
        description: "Weights strategies inversely to their historical volatility.",
        icon: TrendingUp,
        recommended: true
    },
    {
        id: "correlation_penalized",
        title: "Correlation Penalized",
        description: "Reduces exposure to clusters with high pair-wise correlation.",
        icon: Network,
        recommended: false
    },
    {
        id: "capital_protection",
        title: "Capital Protection",
        description: "Prioritizes capital preservation over alpha generation.",
        icon: Shield,
        recommended: false
    }
];

export default function PolicyEditor({ onSave, initialData, onCancel }: PolicyEditorProps) {
    const [formData, setFormData] = useState(initialData || {
        cash_reserve_percent: 20.0,
        daily_stop_loss_percent: 2.0,
        max_equity_exposure_percent: 80.0,
        max_strategy_allocation_percent: 25.0,
        allocation_sensitivity: 'MEDIUM', // Mapped from slider
        correlation_penalty: 'MODERATE', // Mapped from slider
        allocator_sensitivity_val: 0.65, // Internal slider val
        correlation_penalty_val: 2.5 // Internal slider val
    });

    const [selectedMethod, setSelectedMethod] = useState("risk_normalized");

    const handleChange = (field: string, value: any) => {
        setFormData((prev: any) => ({ ...prev, [field]: value }));
    };

    const handleSubmit = () => {
        // Map internal slider values to ENUM strings for backend
        const sensitivity = formData.allocator_sensitivity_val > 0.7 ? "HIGH" : (formData.allocator_sensitivity_val < 0.3 ? "LOW" : "MEDIUM");
        const penalty = formData.correlation_penalty_val > 3.0 ? "HIGH" : (formData.correlation_penalty_val < 1.0 ? "LOW" : "MODERATE");

        onSave({
            ...formData,
            allocation_sensitivity: sensitivity,
            correlation_penalty: penalty
        });
    };

    return (
        <div className="space-y-8">

            {/* Allocation Method Selection */}
            <div>
                <h3 className="text-lg font-bold text-white mb-4 flex items-center gap-2">
                    <span className="bg-cyan-500/10 p-1.5 rounded text-cyan-400"><Scale className="w-4 h-4" /></span>
                    Allocation Logic
                </h3>
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
                    {ALLOCATION_METHODS.map((method) => {
                        const Icon = method.icon;
                        const isSelected = selectedMethod === method.id;
                        return (
                            <div
                                key={method.id}
                                className={cn(
                                    "cursor-pointer rounded-xl border p-5 transition-all relative overflow-hidden group",
                                    isSelected
                                        ? "border-cyan-500 bg-cyan-950/30 text-white shadow-[0_0_15px_rgba(34,211,238,0.1)]"
                                        : "border-white/10 bg-[#0A0A0A]/40 text-gray-400 hover:bg-white/5 hover:border-white/20"
                                )}
                                onClick={() => setSelectedMethod(method.id)}
                            >
                                {isSelected && <div className="absolute inset-0 bg-gradient-to-br from-cyan-500/10 to-transparent pointer-events-none" />}

                                <div className="mb-3 flex items-center justify-between relative z-10">
                                    <div className={cn("p-2 rounded-lg", isSelected ? "bg-cyan-500 text-black" : "bg-white/5 text-gray-400 group-hover:text-white")}>
                                        <Icon className="h-5 w-5" />
                                    </div>
                                    {method.recommended && (
                                        <Badge variant="secondary" className="bg-green-500/20 text-green-400 border-green-500/30">
                                            Recommended
                                        </Badge>
                                    )}
                                </div>
                                <h3 className={cn("font-bold text-sm mb-2 relative z-10", isSelected ? "text-cyan-100" : "text-gray-300 group-hover:text-white")}>
                                    {method.title}
                                </h3>
                                <p className="text-xs text-gray-500 leading-relaxed relative z-10 group-hover:text-gray-400">
                                    {method.description}
                                </p>
                            </div>
                        )
                    })}
                </div>
            </div>

            {/* Policy Settings (Sliders) */}
            <GlassCard className="p-0 overflow-hidden border-t-4 border-t-purple-500">
                <div className="p-6 border-b border-white/10 bg-black/20 flex justify-between items-center">
                    <div>
                        <h3 className="text-lg font-bold text-white flex items-center gap-2">
                            <span className="bg-purple-500/10 p-1.5 rounded text-purple-400"><Shield className="w-4 h-4" /></span>
                            Policy Parameters
                        </h3>
                        <p className="text-sm text-gray-400 ml-8">Fine-tune sensitivity parameters and risk thresholds.</p>
                    </div>
                    <Badge variant="outline" className="text-xs font-normal font-mono border-white/10 text-gray-400">
                        ID: NEXT-GEN-01
                    </Badge>
                </div>

                <div className="p-8 space-y-10">
                    {/* Sliders Grid */}
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-12">
                        {/* Allocator Sensitivity Slider */}
                        <div className="space-y-6">
                            <div className="flex justify-between items-center">
                                <Label className="text-sm font-medium text-gray-300">Allocator Sensitivity</Label>
                                <span className="font-mono text-xs bg-purple-500/20 text-purple-300 px-2 py-1 rounded border border-purple-500/30">
                                    {formData.allocator_sensitivity_val.toFixed(2)}
                                </span>
                            </div>
                            <Slider
                                value={[formData.allocator_sensitivity_val]}
                                max={1.0}
                                step={0.05}
                                onValueChange={(val) => handleChange('allocator_sensitivity_val', val[0])}
                                className="py-2"
                            />
                            <div className="flex justify-between text-[10px] text-gray-500 font-mono uppercase tracking-wider">
                                <span>Conservative</span>
                                <span>Reactive</span>
                            </div>
                        </div>

                        {/* Correlation Penalty Slider */}
                        <div className="space-y-6">
                            <div className="flex justify-between items-center">
                                <Label className="text-sm font-medium text-gray-300">Correlation Penalty Strength</Label>
                                <span className="font-mono text-xs bg-cyan-500/20 text-cyan-300 px-2 py-1 rounded border border-cyan-500/30">
                                    {formData.correlation_penalty_val.toFixed(1)}
                                </span>
                            </div>
                            <Slider
                                value={[formData.correlation_penalty_val]}
                                max={5.0}
                                step={0.1}
                                onValueChange={(val) => handleChange('correlation_penalty_val', val[0])}
                                className="py-2"
                            />
                            <div className="flex justify-between text-[10px] text-gray-500 font-mono uppercase tracking-wider">
                                <span>Off</span>
                                <span>Maximum</span>
                            </div>
                        </div>
                    </div>

                    {/* Numeric Limits */}
                    <div className="grid grid-cols-2 md:grid-cols-4 gap-6 pt-8 border-t border-white/5">
                        <div className="space-y-3">
                            <Label className="text-gray-400 text-xs uppercase tracking-wider">Max Exposure (%)</Label>
                            <input
                                type="number"
                                className="w-full h-10 rounded-lg border border-white/10 bg-black/40 px-3 py-1 text-sm text-white shadow-sm transition-all focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500 focus:outline-none placeholder-gray-600"
                                value={formData.max_equity_exposure_percent}
                                onChange={(e) => handleChange('max_equity_exposure_percent', parseFloat(e.target.value))}
                            />
                        </div>
                        <div className="space-y-3">
                            <Label className="text-gray-400 text-xs uppercase tracking-wider">Cash Reserve (%)</Label>
                            <input
                                type="number"
                                className="w-full h-10 rounded-lg border border-white/10 bg-black/40 px-3 py-1 text-sm text-white shadow-sm transition-all focus:border-green-500 focus:ring-1 focus:ring-green-500 focus:outline-none placeholder-gray-600"
                                value={formData.cash_reserve_percent}
                                onChange={(e) => handleChange('cash_reserve_percent', parseFloat(e.target.value))}
                            />
                        </div>
                        <div className="space-y-3">
                            <Label className="text-red-400 text-xs uppercase tracking-wider flex items-center gap-1">
                                <AlertTriangle className="w-3 h-3" /> Stop Loss (%)
                            </Label>
                            <input
                                type="number"
                                className="w-full h-10 rounded-lg border border-red-500/20 bg-red-950/10 px-3 py-1 text-sm text-red-200 shadow-sm transition-all focus:border-red-500 focus:ring-1 focus:ring-red-500 focus:outline-none placeholder-red-900/50"
                                value={formData.daily_stop_loss_percent}
                                onChange={(e) => handleChange('daily_stop_loss_percent', parseFloat(e.target.value))}
                            />
                        </div>
                        <div className="space-y-3">
                            <Label className="text-gray-400 text-xs uppercase tracking-wider">Max Allocation (%)</Label>
                            <input
                                type="number"
                                className="w-full h-10 rounded-lg border border-white/10 bg-black/40 px-3 py-1 text-sm text-white shadow-sm transition-all focus:border-purple-500 focus:ring-1 focus:ring-purple-500 focus:outline-none placeholder-gray-600"
                                value={formData.max_strategy_allocation_percent}
                                onChange={(e) => handleChange('max_strategy_allocation_percent', parseFloat(e.target.value))}
                            />
                        </div>
                    </div>
                </div>

                <div className="p-6 bg-black/40 border-t border-white/5 flex justify-between">
                    <Button variant="ghost" onClick={onCancel} className="text-gray-400 hover:text-white hover:bg-white/5">Discard Changes</Button>
                    <Button onClick={handleSubmit} className="bg-blue-600 hover:bg-blue-500 text-white shadow-lg shadow-blue-900/20 border-t border-blue-400/20">
                        <Save className="mr-2 h-4 w-4" />
                        Save Configuration
                    </Button>
                </div>
            </GlassCard>
        </div>
    );
}
