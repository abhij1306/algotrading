"use client";

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';

export default function QuantRedirectPage() {
    const router = useRouter();

    useEffect(() => {
        router.push('/quant/monitoring');
    }, [router]);

    return (
        <div className="flex items-center justify-center h-full w-full">
            <div className="text-gray-500 text-sm animate-pulse tracking-widest uppercase font-mono">Initializing Quant Lab...</div>
        </div>
    );
}
