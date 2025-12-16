'use client'

import { useState, useEffect } from 'react';
import { Newspaper, RefreshCw, ExternalLink, Clock } from 'lucide-react';

interface NewsItem {
    id: number;
    title: string;
    summary: string;
    source: string;
    url: string;
    published_at: string;
    symbols: string[];
    sentiment: string;
}

export default function NewsTicker() {
    const [news, setNews] = useState<NewsItem[]>([]);
    const [loading, setLoading] = useState(false);
    const [lastUpdate, setLastUpdate] = useState<Date | null>(null);

    const fetchNews = async () => {
        try {
            setLoading(true);
            const response = await fetch('http://localhost:8000/api/news/latest?limit=50');
            const data = await response.json();

            if (data.success) {
                setNews(data.news);
                setLastUpdate(new Date());
            }
        } catch (error) {
            console.error('Error fetching news:', error);
        } finally {
            setLoading(false);
        }
    };

    const refreshNews = async () => {
        try {
            setLoading(true);
            // Trigger news fetch from sources
            await fetch('http://localhost:8000/api/news/refresh', { method: 'POST' });
            // Then fetch latest
            await fetchNews();
        } catch (error) {
            console.error('Error refreshing news:', error);
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        // Initial fetch
        fetchNews();

        // Auto-refresh every 30 seconds
        const interval = setInterval(fetchNews, 30000);

        return () => clearInterval(interval);
    }, []);

    const getTimeAgo = (publishedAt: string) => {
        const now = new Date();
        const published = new Date(publishedAt);

        const diffMs = now.getTime() - published.getTime();
        const diffMins = Math.floor(diffMs / 60000);

        if (diffMins < 1) return 'Just now';
        if (diffMins < 60) return `${diffMins}m ago`;

        const diffHours = Math.floor(diffMins / 60);
        if (diffHours < 24) return `${diffHours}h ago`;

        const diffDays = Math.floor(diffHours / 24);
        return `${diffDays}d ago`;
    };


    const getSentimentColor = (sentiment: string) => {
        switch (sentiment) {
            case 'positive': return 'text-green-400';
            case 'negative': return 'text-red-400';
            default: return 'text-text-secondary';
        }
    };

    return (
        <div className="w-80 h-screen bg-card-dark border-l border-border-dark flex flex-col">
            {/* Header */}
            <div className="p-4 border-b border-border-dark">
                <div className="flex items-center justify-between mb-2">
                    <div className="flex items-center gap-2">
                        <Newspaper className="w-5 h-5 text-primary" />
                        <h2 className="text-lg font-bold text-white">Market News</h2>
                    </div>
                    <button
                        onClick={refreshNews}
                        disabled={loading}
                        className="p-2 hover:bg-white/5 rounded-lg transition-colors disabled:opacity-50"
                        title="Refresh news"
                    >
                        <RefreshCw className={`w-4 h-4 text-text-secondary ${loading ? 'animate-spin' : ''}`} />
                    </button>
                </div>
                {lastUpdate && (
                    <div className="flex items-center gap-1 text-xs text-text-secondary">
                        <Clock className="w-3 h-3" />
                        <span>Updated {getTimeAgo(lastUpdate.toISOString())}</span>
                    </div>
                )}
            </div>

            {/* News List */}
            <div className="flex-1 overflow-y-auto">
                {loading && news.length === 0 ? (
                    <div className="flex items-center justify-center h-32">
                        <RefreshCw className="w-6 h-6 text-text-secondary animate-spin" />
                    </div>
                ) : news.length === 0 ? (
                    <div className="p-4 text-center text-text-secondary">
                        <Newspaper className="w-12 h-12 mx-auto mb-2 opacity-50" />
                        <p>No news available</p>
                        <button
                            onClick={refreshNews}
                            className="mt-2 text-sm text-primary hover:underline"
                        >
                            Fetch latest news
                        </button>
                    </div>
                ) : (
                    <div className="divide-y divide-border-dark">
                        {news.map((item) => (
                            <div
                                key={item.id}
                                className="p-4 hover:bg-white/5 transition-colors cursor-pointer group"
                                onClick={() => window.open(item.url, '_blank')}
                            >
                                {/* Time and Source */}
                                <div className="flex items-center justify-between mb-2">
                                    <span className="text-xs text-text-secondary">
                                        {getTimeAgo(item.published_at)}
                                    </span>
                                    <span className="text-xs text-text-secondary">
                                        {item.source}
                                    </span>
                                </div>

                                {/* Title */}
                                <h3 className={`text-sm font-medium mb-1 line-clamp-2 group-hover:text-primary transition-colors ${getSentimentColor(item.sentiment)}`}>
                                    {item.title}
                                </h3>

                                {/* Summary */}
                                {item.summary && item.summary !== item.title && (
                                    <p className="text-xs text-text-secondary mb-2 line-clamp-2">
                                        {item.summary}
                                    </p>
                                )}
                                {item.symbols && item.symbols.length > 0 && (
                                    <div className="flex flex-wrap gap-1">
                                        {item.symbols.slice(0, 3).map((symbol) => (
                                            <span
                                                key={symbol}
                                                className="px-2 py-0.5 text-xs bg-primary/20 text-primary rounded-full"
                                            >
                                                #{symbol}
                                            </span>
                                        ))}
                                        {item.symbols.length > 3 && (
                                            <span className="px-2 py-0.5 text-xs text-text-secondary">
                                                +{item.symbols.length - 3}
                                            </span>
                                        )}
                                    </div>
                                )}

                                {/* External link icon */}
                                <ExternalLink className="w-3 h-3 text-text-secondary opacity-0 group-hover:opacity-100 transition-opacity absolute top-4 right-4" />
                            </div>
                        ))}
                    </div>
                )}
            </div>
        </div>
    );
}
