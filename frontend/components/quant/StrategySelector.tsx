"use client";

import React, { useState, useEffect } from 'react';
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Search, GripVertical, Plus, AlertCircle } from "lucide-react";
import { cn } from "@/lib/utils";

// Removed Mock Data. Fetching from API.

export default function StrategySelector({ onSelect }: { onSelect: (strategy: any) => void }) {
    const [searchQuery, setSearchQuery] = useState("");
    const [strategies, setStrategies] = useState<any[]>([]);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        fetch('http://localhost:8000/api/portfolio/strategies/available')
            .then(res => res.json())
            .then(data => {
                setStrategies(data);
                setLoading(false);
            })
            .catch(err => {
                console.error("Failed to fetch strategies", err);
                setStrategies([]); // Ensure empty state
                setLoading(false);
            });
    }, []);

    const filteredStrategies = strategies.filter(s =>
        (s.name?.toLowerCase().includes(searchQuery.toLowerCase())) ||
        (s.type?.toLowerCase().includes(searchQuery.toLowerCase()))
    );

    return (
        <Card className="h-full border-t-4 border-t-amber-500">
            <CardHeader>
                <CardTitle className="flex justify-between items-center">
                    <span>Strategy Library</span>
                    <Badge variant="outline">{strategies.length} Available</Badge>
                </CardTitle>
                <div className="relative">
                    <Search className="absolute left-2 top-2.5 h-4 w-4 text-muted-foreground" />
                    <input
                        placeholder="Search strategies..."
                        className="w-full rounded-md border border-input bg-background px-8 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        value={searchQuery}
                        onChange={(e) => setSearchQuery(e.target.value)}
                    />
                </div>
            </CardHeader>
            <CardContent className="p-0">
                <ScrollArea className="h-[400px] px-6 pb-6">
                    {loading ? (
                        <div className="flex items-center justify-center h-40 text-muted-foreground">
                            Loading Library...
                        </div>
                    ) : filteredStrategies.length === 0 ? (
                        <div className="flex flex-col items-center justify-center h-40 text-muted-foreground space-y-2">
                            <AlertCircle className="h-8 w-8 opacity-50" />
                            <p>No strategies found.</p>
                            <Button variant="outline" size="sm">Import Strategy</Button>
                        </div>
                    ) : (
                        <div className="space-y-3">
                            {filteredStrategies.map((strategy) => (
                                <div
                                    key={strategy.id}
                                    className="group flex items-center justify-between rounded-lg border p-3 hover:bg-accent cursor-pointer transition-colors"
                                    onClick={() => onSelect(strategy)}
                                >
                                    <div className="flex items-center gap-3">
                                        <GripVertical className="h-5 w-5 text-muted-foreground opacity-50 group-hover:opacity-100" />
                                        <div>
                                            <div className="font-medium text-sm">{strategy.name}</div>
                                            <div className="text-xs text-muted-foreground">{strategy.type} • {strategy.description}</div>
                                        </div>
                                    </div>
                                    <div className="flex items-center gap-2">
                                        <Badge variant="secondary" className="text-[10px]">
                                            {strategy.risk || 'N/A'}
                                        </Badge>
                                        <Button size="icon" variant="ghost" className="h-8 w-8">
                                            <Plus className="h-4 w-4" />
                                        </Button>
                                    </div>
                                </div>
                            ))}
                        </div>
                    )}
                </ScrollArea>
            </CardContent>
        </Card>
    );
}
