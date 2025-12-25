
"use client";

import React, { useEffect, useState } from 'react';
import { GlassCard } from "@/components/ui/GlassCard";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { FlaskConical, Play, MoreHorizontal, Plus } from "lucide-react";
import Link from 'next/link';

export default function PortfolioList() {
    const [portfolios, setPortfolios] = useState<any[]>([]);
    const [isEditing, setIsEditing] = useState(false);

    // Lazy load if needed, or better yet, import at top if standard component
    // const PortfolioEditor = require('./PortfolioEditor').default;
    // Actually, let's use standard dynamic import if possible, or just require if Next.js context allows easily.
    // Given the previous pattern used require, sticking to it for consistency in this file context, 
    // though top-level import is preferred. I'll use require since I can't easily add top-level import without replacing the whole file header.
    const PortfolioEditor = require('./PortfolioEditor').default; // Dynamic require to match PolicyList pattern

    useEffect(() => {
        fetchPortfolios();
    }, []);

    const fetchPortfolios = () => {
        fetch('http://localhost:9000/api/portfolio/strategies')
            .then(res => res.json())
            .then(data => setPortfolios(data))
            .catch(err => console.error("Failed to fetch portfolios", err));
    };

    const handleCreate = async (portfolio: any) => {
        try {
            const res = await fetch('http://localhost:9000/api/portfolio/strategies', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(portfolio)
            });
            if (res.ok) {
                fetchPortfolios();
                setIsEditing(false);
            }
        } catch (e) {
            console.error("Failed to create portfolio", e);
        }
    };

    if (isEditing) {
        return (
            <PortfolioEditor
                onCancel={() => setIsEditing(false)}
                onSave={handleCreate}
            />
        );
    }

    return (
        <div className="space-y-6">
            <div className="flex justify-end">
                <Button
                    onClick={() => setIsEditing(true)}
                    className="bg-purple-600 hover:bg-purple-500 text-white shadow-lg shadow-purple-900/20 border-t border-purple-400/20"
                >
                    <Plus className="mr-2 h-4 w-4" />
                    New Experiment
                </Button>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                {portfolios.length === 0 && (
                    <div className="col-span-full py-16 text-center">
                        <div className="bg-purple-500/10 w-16 h-16 rounded-full flex items-center justify-center mx-auto mb-4">
                            <FlaskConical className="w-8 h-8 text-purple-400" />
                        </div>
                        <h3 className="text-xl font-bold text-white mb-2">No Research Portfolios</h3>
                        <p className="text-gray-400 max-w-md mx-auto mb-6">
                            Start a new experiment to test multi-strategy combinations against historical data.
                        </p>
                        <Button onClick={() => setIsEditing(true)} className="bg-purple-600 hover:bg-purple-500">
                            <Plus className="mr-2 h-4 w-4" />
                            Create Portfolio
                        </Button>
                    </div>
                )}

                {portfolios.map((portfolio) => (
                    <GlassCard key={portfolio.id} className="relative group overflow-hidden border-t-2 border-t-purple-500 hover:border-t-purple-400 transition-all duration-300">
                        {/* Hover Gradient */}
                        <div className="absolute inset-0 bg-gradient-to-br from-purple-500/5 to-transparent opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none" />

                        <div className="p-6">
                            <div className="flex justify-between items-start mb-4">
                                <div className="flex items-center gap-3">
                                    <div className="p-2 bg-purple-500/10 rounded-lg text-purple-400">
                                        <FlaskConical className="h-5 w-5" />
                                    </div>
                                    <div>
                                        <h3 className="font-bold text-lg text-white group-hover:text-purple-300 transition-colors">
                                            {portfolio.name}
                                        </h3>
                                        <span className="text-[10px] text-gray-500 font-mono">{portfolio.benchmark || "NIFTY 50"}</span>
                                    </div>
                                </div>
                                <Badge variant="secondary" className={
                                    portfolio.status === 'LIVE'
                                        ? "bg-green-500/20 text-green-400 border-green-500/30"
                                        : "bg-white/5 text-gray-400 border-white/10"
                                }>
                                    {portfolio.status}
                                </Badge>
                            </div>

                            <p className="text-sm text-gray-400 mb-6 line-clamp-2 min-h-[40px]">
                                {portfolio.description || "No description provided."}
                            </p>

                            <div className="space-y-3 pt-4 border-t border-white/5">
                                <div className="flex justify-between text-xs text-gray-500 uppercase tracking-wider">
                                    <span>Strategies</span>
                                    <span className="font-mono text-white">{portfolio.composition?.length || 0}</span>
                                </div>
                                <div className="flex justify-between text-xs text-gray-500 uppercase tracking-wider">
                                    <span>Last Run</span>
                                    <span className="font-mono text-white">
                                        {portfolio.metrics_snapshot ? "Has Results" : "Never"}
                                    </span>
                                </div>
                            </div>
                        </div>

                        <div className="p-4 bg-black/20 border-t border-white/5 flex gap-2">
                            <Button className="flex-1 bg-purple-600/80 hover:bg-purple-600 text-sm h-9">
                                <Play className="mr-2 h-3 w-3" />
                                Run Backtest
                            </Button>
                            <Button variant="outline" size="icon" className="h-9 w-9 border-white/10 hover:bg-white/5 text-gray-400 hover:text-white">
                                <MoreHorizontal className="h-4 w-4" />
                            </Button>
                        </div>
                    </GlassCard>
                ))}
            </div>
        </div>
    );
}
