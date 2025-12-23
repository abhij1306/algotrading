'use client';

import { ReactNode } from 'react';
// import Navbar from '@/components/Navbar'; // Replaced by UnifiedSidebar
import { CommandPalette } from '@/components/CommandPalette';

interface AppLayoutProps {
    children: ReactNode;
}

import UnifiedSidebar from '@/components/layout/UnifiedSidebar';

interface AppLayoutProps {
    children: ReactNode;
}

export default function AppLayout({ children }: AppLayoutProps) {
    return (
        <div className="flex h-screen bg-deep-space text-gray-200 overflow-hidden selection:bg-cyan-500/30 selection:text-cyan-100">
            {/* GLOBAL SIDEBAR */}
            <UnifiedSidebar />

            {/* MAIN CONTENT AREA */}
            <main className="flex-1 flex flex-col min-w-0 overflow-hidden relative z-0">
                {/* Global Command Palette (Hit Ctrl+K) */}
                <CommandPalette />

                {/* Page Content */}
                <div className="flex-1 overflow-auto">
                    {children}
                </div>
            </main>
        </div>
    );
}
