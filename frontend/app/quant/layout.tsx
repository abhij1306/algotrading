"use client";

import { ReactNode } from "react";

export default function QuantLayout({ children }: { children: ReactNode }) {
    return (
        <div className="h-full w-full bg-[#050505] overflow-hidden relative">
            {children}
        </div>
    );
}
