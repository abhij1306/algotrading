'use client'

import { useState, useEffect } from 'react';
import { Check, X, AlertCircle, Zap, ShieldCheck } from 'lucide-react';

interface PendingAction {
    id: number;
    source_agent: string;
    action_type: string;
    payload: {
        symbol: string;
        direction: string;
        quantity: number;
        entry_price: number;
    };
    reason: string;
    confidence: number;
    created_at: string;
}

export default function ActionCenter() {
    const [actions, setActions] = useState<PendingAction[]>([]);
    const [loading, setLoading] = useState(true);
    const [processingId, setProcessingId] = useState<number | null>(null);

    const fetchActions = async () => {
        try {
            const res = await fetch('http://localhost:8000/api/actions/pending');
            const data = await res.json();
            setActions(data.actions || []);
        } catch (err) {
            console.error("Failed to fetch pending actions", err);
        } finally {
            setLoading(false);
        }
    };

    const handleAction = async (id: number, type: 'approve' | 'reject') => {
        setProcessingId(id);
        try {
            const res = await fetch(`http://localhost:8000/api/actions/${id}/${type}`, {
                method: 'POST'
            });
            const data = await res.json();
            if (data.status === 'success') {
                setActions(actions.filter(a => a.id !== id));
            } else {
                alert(`Error: ${data.message}`);
            }
        } catch (err) {
            console.error(`Failed to ${type} action`, err);
        } finally {
            setProcessingId(null);
        }
    };

    useEffect(() => {
        fetchActions();
        const interval = setInterval(fetchActions, 5000);
        return () => clearInterval(interval);
    }, []);

    if (loading && actions.length === 0) {
        return <div className="p-4 text-center opacity-50 text-xs">Loading pending reviews...</div>;
    }

    return (
        <div className="flex flex-col gap-3 p-3">
            <div className="flex items-center justify-between mb-1 px-1">
                <span className="text-[10px] font-bold text-gray-500 uppercase tracking-widest flex items-center gap-1.5">
                    <ShieldCheck className="w-3 h-3 text-cyan-400" /> Human Review Required
                </span>
                <span className="text-[10px] bg-cyan-500/10 text-cyan-400 px-1.5 py-0.5 rounded border border-cyan-500/20">
                    {actions.length} PENDING
                </span>
            </div>

            {actions.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-10 opacity-30">
                    <Check className="w-8 h-8 mb-2" />
                    <p className="text-[10px] uppercase font-bold">All clear</p>
                </div>
            ) : (
                actions.map(action => (
                    <div key={action.id} className="bg-card-dark border border-white/10 rounded-xl p-3 relative overflow-hidden group hover:border-cyan-500/30 transition-all shadow-lg">
                        {/* Status Bar */}
                        <div className={`absolute left-0 top-0 bottom-0 w-1 ${action.payload.direction === 'LONG' ? 'bg-blue-500' : 'bg-red-500'}`} />

                        <div className="flex justify-between items-start mb-2 pl-1">
                            <div>
                                <h4 className="text-sm font-bold text-white tracking-tight">{action.payload.symbol}</h4>
                                <div className="flex items-center gap-2 mt-1">
                                    <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded border uppercase ${action.payload.direction === 'LONG' ? 'bg-blue-500/10 border-blue-500/30 text-blue-400' : 'bg-red-500/10 border-red-500/30 text-red-400'
                                        }`}>
                                        {action.payload.direction}
                                    </span>
                                    <span className="text-[9px] text-gray-500 font-mono italic">{action.source_agent}</span>
                                </div>
                            </div>
                            <div className="text-right">
                                <div className="text-sm font-bold text-cyan-400">{Math.round(action.confidence)}%</div>
                                <div className="text-[9px] text-gray-500 uppercase tracking-tighter">Confidence</div>
                            </div>
                        </div>

                        <div className="bg-black/20 rounded p-2 mb-3 ml-1 border border-white/5">
                            <p className="text-[10px] text-gray-300 leading-relaxed font-medium">
                                {action.reason}
                            </p>
                            <div className="mt-2 flex items-center justify-between">
                                <span className="text-[9px] text-gray-500">Qty: <span className="text-gray-300 font-bold">{action.payload.quantity}</span></span>
                                <span className="text-[9px] text-gray-500">Price: <span className="text-gray-300 font-bold">₹{action.payload.entry_price}</span></span>
                            </div>
                        </div>

                        <div className="flex gap-2 ml-1">
                            <button
                                onClick={() => handleAction(action.id, 'approve')}
                                disabled={processingId === action.id}
                                className="flex-1 py-1.5 bg-cyan-600 hover:bg-cyan-500 text-white rounded-lg text-xs font-bold transition-all flex items-center justify-center gap-1.5 shadow-lg shadow-cyan-900/20 disabled:opacity-50"
                            >
                                <Zap className="w-3 h-3 fill-current" /> Approve
                            </button>
                            <button
                                onClick={() => handleAction(action.id, 'reject')}
                                disabled={processingId === action.id}
                                className="px-3 py-1.5 bg-red-500/10 hover:bg-red-500/20 text-red-400 border border-red-500/30 rounded-lg text-xs font-bold transition-all flex items-center justify-center disabled:opacity-50"
                            >
                                <X className="w-3.5 h-3.5" />
                            </button>
                        </div>
                    </div>
                ))
            )}
        </div>
    );
}
