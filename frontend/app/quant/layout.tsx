"use client";

import { ReactNode } from "react";

export default function QuantLayout({ children }: { children: ReactNode }) {
    return (
        <div className="h-full w-full bg-[var(--color-base)] overflow-hidden relative">
            {children}
        </div>
    );
}
