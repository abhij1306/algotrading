"use client";

import React, { useState, useEffect } from 'react';
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { GripVertical, Plus, X, AlertTriangle, ArrowRight } from "lucide-react";
import { cn } from "@/lib/utils";

// Define prop types
interface StrategyAllocationProps {
    onCompositionChange: (ids: string[]) => void;
    onValidationComplete?: (results: any) => void;
}

export default function StrategyAllocation({ onCompositionChange, onValidationComplete }: StrategyAllocationProps) {
    const [library, setLibrary] = useState<any[]>([]);
    const [selected, setSelected] = useState<any[]>([]);
    const [correlationWarning, setCorrelationWarning] = useState<string | null>(null);

    // Fetch Policies
    const [policies, setPolicies] = useState<any[]>([]);
    const [selectedPolicyId, setSelectedPolicyId] = useState<string>("");
    const [isRunning, setIsRunning] = useState(false);

    // Fetch Library
    useEffect(() => {
        fetch('http://localhost:8000/api/portfolio/strategies/available')
            .then(res => res.json())
            .then(data => setLibrary(data))
            .catch(console.error);
    }, []);

    // Fetch Policies
    useEffect(() => {
        fetch('http://localhost:8000/api/portfolio/strategies/policy')
            .then(res => res.json())
            .then(data => {
                setPolicies(data);
                if (data.length > 0) setSelectedPolicyId(data[0].id);
            })
            .catch(console.error);
    }, []);

    // Check Correlation
    useEffect(() => {
        if (selected.length < 2) {
            setCorrelationWarning(null);
            onCompositionChange(selected.map(s => s.strategy_id));
            return;
        }

        const ids = selected.map(s => s.strategy_id);
        fetch('http://localhost:8000/api/portfolio/strategies/correlation', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ strategy_ids: ids })
        })
            .then(res => res.json())
            .then(data => {
                if (data.max_correlation > 0.7) {
                    setCorrelationWarning("High Correlation Detected (> 0.7). Low Diversification Benefit.");
                } else {
                    setCorrelationWarning(null);
                }
            })
            .catch(console.error);

        onCompositionChange(ids);
    }, [selected]);

    const addToPortfolio = (strategy: any) => {
        if (selected.find(s => s.strategy_id === strategy.strategy_id)) return;
        setSelected([...selected, strategy]);
    };

    const removeFromPortfolio = (id: string) => {
        setSelected(selected.filter(s => s.strategy_id !== id));
    };

    const handleRunSimulation = async () => {
        if (!selectedPolicyId || selected.length === 0) return;

        setIsRunning(true);
        try {
            const payload = {
                policy_id: selectedPolicyId,
                strategy_ids: selected.map(s => s.strategy_id),
                start_date: "2024-01-01",
                end_date: "2024-12-31"
            };

            const res = await fetch('http://localhost:8000/api/portfolio/strategies/backtest', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            if (!res.ok) throw new Error("Simulation failed");

            const results = await res.json();
            // Propagate up to parent
            if (onValidationComplete) onValidationComplete(results);

        } catch (err) {
            console.error(err);
        } finally {
            setIsRunning(false);
        }
    };

    return (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6 h-[600px]">
            {/* Library Column */}
            <Card className="flex flex-col h-full border-t-4 border-t-cyan-500/50 md:col-span-1">
                <CardHeader>
                    <CardTitle className="text-lg">Strategy Library</CardTitle>
                </CardHeader>
                <CardContent className="flex-1 overflow-hidden p-0">
                    <ScrollArea className="h-full px-6 pb-6">
                        <div className="space-y-3">
                            {library.map(s => (
                                <div key={s.strategy_id}
                                    className="flex items-center justify-between p-3 border rounded-lg bg-card hover:bg-accent/50 cursor-pointer group transition-all"
                                    onClick={() => addToPortfolio(s)}>
                                    <div className="flex items-center gap-3">
                                        <div className={`w-1 h-8 rounded-full ${colors[s.regime] || 'bg-gray-500'}`} />
                                        <div>
                                            <div className="font-medium text-sm">{s.strategy_id}</div>
                                            <div className="text-xs text-muted-foreground truncate w-40">{s.description}</div>
                                        </div>
                                    </div>
                                    <Plus className="h-4 w-4 opacity-0 group-hover:opacity-100 transition-opacity" />
                                </div>
                            ))}
                        </div>
                    </ScrollArea>
                </CardContent>
            </Card>

            {/* Portfolio Bucket Column */}
            <Card className="flex flex-col h-full border-t-4 border-t-pink-500/50 relative bg-gradient-to-br from-background to-pink-900/5 md:col-span-2">
                <CardHeader className="flex flex-row items-center justify-between">
                    <CardTitle className="text-lg">Research Portfolio</CardTitle>
                    <div className="flex gap-2 items-center">
                        <Badge variant="outline">{selected.length} Strategies</Badge>
                        {/* Policy Selector */}
                        <select
                            className="h-8 w-40 rounded-md border text-xs bg-background"
                            value={selectedPolicyId}
                            onChange={(e) => setSelectedPolicyId(e.target.value)}
                        >
                            {policies.map(p => (
                                <option key={p.id} value={p.id}>
                                    Policy: {p.id.substring(0, 8)}...
                                </option>
                            ))}
                        </select>
                        <Button
                            size="sm"
                            className="bg-pink-600 hover:bg-pink-500"
                            disabled={selected.length === 0 || isRunning}
                            onClick={handleRunSimulation}
                        >
                            {isRunning ? "Simulating..." : "Run Test"}
                            {!isRunning && <ArrowRight className="ml-2 h-4 w-4" />}
                        </Button>
                    </div>
                </CardHeader>
                <CardContent className="flex-1 overflow-hidden px-6 pb-6 space-y-4">
                    {correlationWarning && (
                        <div className="flex items-center gap-2 p-3 bg-yellow-500/10 border border-yellow-500/20 rounded-md text-yellow-500 text-xs font-bold animate-in fade-in slide-in-from-top-2">
                            <AlertTriangle className="h-4 w-4" />
                            {correlationWarning}
                        </div>
                    )}

                    <ScrollArea className="h-full pr-4">
                        <div className="space-y-3">
                            {selected.length === 0 ? (
                                <div className="h-40 flex items-center justify-center text-muted-foreground border-2 border-dashed rounded-lg">
                                    Select strategies to begin
                                </div>
                            ) : selected.map((s, index) => (
                                <div key={s.strategy_id} className="flex items-center justify-between p-3 border border-pink-500/20 bg-pink-500/5 rounded-lg">
                                    <div className="flex items-center gap-3">
                                        <Badge className="bg-pink-500/20 text-pink-300 hover:bg-pink-500/30">{index + 1}</Badge>
                                        <div>
                                            <div className="font-bold text-sm">{s.strategy_id}</div>
                                            <div className="text-xs text-muted-foreground">Regime: {s.regime}</div>
                                        </div>
                                    </div>
                                    <Button size="icon" variant="ghost" className="h-6 w-6 text-red-400 hover:text-red-300" onClick={() => removeFromPortfolio(s.strategy_id)}>
                                        <X className="h-4 w-4" />
                                    </Button>
                                </div>
                            ))}
                        </div>
                    </ScrollArea>
                </CardContent>
            </Card>
        </div>
    );
}

const colors: Record<string, string> = {
    'VOLATILITY': 'bg-purple-500',
    'TREND': 'bg-green-500',
    'RANGE': 'bg-blue-500',
    'NEUTRAL': 'bg-gray-500'
};
