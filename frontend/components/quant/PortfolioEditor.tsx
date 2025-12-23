"use client";

import React, { useState } from 'react';
import { GlassCard } from "@/components/ui/GlassCard";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { GlassSelect } from "@/components/ui/GlassSelect";
import { Save, Briefcase, BarChart4, PieChart, Layers, ArrowLeft } from "lucide-react";
import { Badge } from "@/components/ui/badge";

interface PortfolioEditorProps {
    onSave: (portfolio: any) => void;
    initialData?: any;
    onCancel: () => void;
}

const NIFTY_INDICES = [
    { id: "NIFTY 50", name: "Nifty 50", description: "Top 50 companies listed on NSE" },
    { id: "NIFTY BANK", name: "Nifty Bank", description: "Liquid and large capitalised Indian Banking stocks" },
    { id: "NIFTY FIN SERVICE", name: "Nifty Fin Service", description: "Financial Service companies" },
    { id: "NIFTY MIDCAP 50", name: "Nifty Midcap 50", description: "Top 50 midcap companies" },
    { id: "NIFTY IT", name: "Nifty IT", description: "Information Technology companies" },
    { id: "NIFTY AUTO", name: "Nifty Auto", description: "Automobile sector" },
    { id: "NIFTY METAL", name: "Nifty Metal", description: "Metal sector" },
    { id: "NIFTY PHARMA", name: "Nifty Pharma", description: "Pharmaceutical sector" },
    { id: "NIFTY FMCG", name: "Nifty FMCG", description: "Fast Moving Consumer Goods" },
    { id: "NIFTY NEXT 50", name: "Nifty Next 50", description: "Next 50 large cap companies" }
];

export default function PortfolioEditor({ onSave, initialData, onCancel }: PortfolioEditorProps) {
    const [formData, setFormData] = useState(initialData || {
        name: "",
        description: "",
        benchmark: "NIFTY 50",
        capital: 1000000,
        type: "EQUITY"
    });

    const handleChange = (field: string, value: any) => {
        setFormData((prev: any) => ({ ...prev, [field]: value }));
    };

    const handleSubmit = () => {
        if (!formData.name) {
            alert("Portfolio name is required");
            return;
        }
        onSave({
            ...formData,
            id: initialData?.id // Preserve ID if editing
        });
    };

    return (
        <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
            <div className="flex items-center justify-between">
                <Button
                    variant="ghost"
                    onClick={onCancel}
                    className="text-gray-400 hover:text-white hover:bg-white/5 pl-0 gap-2"
                >
                    <ArrowLeft className="h-4 w-4" /> Back to Portfolios
                </Button>
                <h2 className="text-xl font-bold text-white tracking-tight flex items-center gap-3">
                    <span className="bg-gradient-to-br from-purple-600 to-blue-600 p-2 rounded-lg text-white shadow-lg shadow-purple-900/20">
                        <Briefcase className="h-5 w-5" />
                    </span>
                    {initialData ? "Edit Portfolio" : "Create New Portfolio"}
                </h2>
            </div>

            <GlassCard className="p-0 overflow-hidden border-t-4 border-t-purple-500">
                <div className="p-8 space-y-8">
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
                        {/* Basic Info */}
                        <div className="space-y-6">
                            <h3 className="text-lg font-bold text-white flex items-center gap-2 pb-2 border-b border-white/5">
                                <span className="bg-purple-500/10 p-1.5 rounded text-purple-400"><Layers className="w-4 h-4" /></span>
                                General Information
                            </h3>

                            <div className="space-y-4">
                                <div className="space-y-2">
                                    <Label className="text-gray-400 text-xs uppercase tracking-wider">Portfolio Name</Label>
                                    <Input
                                        placeholder="e.g. Nifty Alpha Strategy"
                                        value={formData.name}
                                        onChange={(e) => handleChange('name', e.target.value)}
                                        className="bg-black/40 border-white/10 text-white focus:border-purple-500 transition-all font-medium py-6"
                                    />
                                </div>

                                <div className="space-y-2">
                                    <Label className="text-gray-400 text-xs uppercase tracking-wider">Description</Label>
                                    <Textarea
                                        placeholder="Describe the objective of this portfolio..."
                                        value={formData.description}
                                        onChange={(e) => handleChange('description', e.target.value)}
                                        className="bg-black/40 border-white/10 text-white focus:border-purple-500 min-h-[100px]"
                                    />
                                </div>
                            </div>
                        </div>

                        {/* Configuration */}
                        <div className="space-y-6">
                            <h3 className="text-lg font-bold text-white flex items-center gap-2 pb-2 border-b border-white/5">
                                <span className="bg-cyan-500/10 p-1.5 rounded text-cyan-400"><PieChart className="w-4 h-4" /></span>
                                Configuration
                            </h3>

                            <div className="space-y-4">
                                <div className="space-y-2">
                                    <Label className="text-gray-400 text-xs uppercase tracking-wider">Benchmark Index (Universe)</Label>
                                    <GlassSelect
                                        value={formData.benchmark}
                                        onChange={(val) => handleChange('benchmark', val)}
                                        options={NIFTY_INDICES.map(idx => ({
                                            value: idx.id,
                                            label: idx.name
                                        }))}
                                        placeholder="Select Index"
                                        className="h-12 bg-black/40 border-white/10 text-white w-full"
                                    />
                                </div>

                                <div className="space-y-2">
                                    <Label className="text-gray-400 text-xs uppercase tracking-wider">Initial Capital</Label>
                                    <div className="relative">
                                        <span className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500 font-mono">₹</span>
                                        <Input
                                            type="number"
                                            value={formData.capital}
                                            onChange={(e) => handleChange('capital', parseFloat(e.target.value))}
                                            className="bg-black/40 border-white/10 text-white pl-8 focus:border-green-500 font-mono h-12"
                                        />
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>

                    <div className="flex items-center gap-4 p-4 bg-blue-500/5 border border-blue-500/20 rounded-xl">
                        <div className="p-2 bg-blue-500/10 rounded-full shrink-0">
                            <BarChart4 className="w-5 h-5 text-blue-400" />
                        </div>
                        <p className="text-sm text-blue-200">
                            Creating a portfolio will initialize a new research environment. You can then add strategies and run backtests against the selected <strong>{formData.benchmark}</strong> universe.
                        </p>
                    </div>
                </div>

                <div className="p-6 bg-black/40 border-t border-white/5 flex justify-end gap-3">
                    <Button variant="ghost" onClick={onCancel} className="text-gray-400 hover:text-white hover:bg-white/5">Cancel</Button>
                    <Button onClick={handleSubmit} className="bg-purple-600 hover:bg-purple-500 text-white shadow-lg shadow-purple-900/20 min-w-[150px]">
                        <Save className="mr-2 h-4 w-4" />
                        Create Portfolio
                    </Button>
                </div>
            </GlassCard>
        </div>
    );
}
