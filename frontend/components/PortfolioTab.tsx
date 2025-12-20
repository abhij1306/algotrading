'use client'

import { useState, useEffect } from 'react'
import PortfolioRiskDashboard from './PortfolioRiskDashboard'
import PortfolioInput from './PortfolioInput'

export default function PortfolioTab() {
    const [portfolios, setPortfolios] = useState<any[]>([])
    const [selectedPortfolioId, setSelectedPortfolioId] = useState<number | null>(null)
    const [loading, setLoading] = useState(true)

    // Fetch portfolios on mount
    useEffect(() => {
        fetchPortfolios()
    }, [])

    const fetchPortfolios = async () => {
        try {
            const res = await fetch('http://localhost:8000/api/portfolios')
            const data = await res.json()
            setPortfolios(data.portfolios || [])

            // Auto-select first portfolio
            if (data.portfolios && data.portfolios.length > 0) {
                setSelectedPortfolioId(data.portfolios[0].id)
            }
        } catch (e) {
            console.error('Failed to fetch portfolios:', e)
        } finally {
            setLoading(false)
        }
    }

    // Show PortfolioInput if no portfolios
    if (!loading && portfolios.length === 0) {
        return (
            <div className="h-full">
                <PortfolioInput onPortfolioCreated={(id: number) => {
                    fetchPortfolios();
                    setSelectedPortfolioId(id);
                }} />
            </div>
        )
    }

    // Show dashboard if portfolios exist
    return (
        <div className="h-[calc(100vh-140px)] flex flex-col">
            <div className="flex-1 overflow-hidden rounded-xl border border-white/5 bg-obsidian-depth">
                {selectedPortfolioId ? (
                    <PortfolioRiskDashboard
                        portfolioId={selectedPortfolioId}
                        portfolios={portfolios}
                        onSelectPortfolio={setSelectedPortfolioId}
                    />
                ) : (
                    <div className="flex items-center justify-center h-full text-text-secondary">
                        {loading ? 'Loading portfolios...' : 'Select a portfolio to view analysis'}
                    </div>
                )}
            </div>
        </div>
    )
}
