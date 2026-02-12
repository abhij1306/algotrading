import PolicyList from "@/components/quant/PolicyList";

export const metadata = {
    title: "Risk Governance | SmartTrader",
    description: "Configure portfolio risk policies and governance rules",
};

export default function GovernancePage() {
    return (
        <div className="h-full w-full p-6 overflow-auto">
            <div className="mb-6">
                <h1 className="text-xl font-bold text-[var(--text-primary)] tracking-tight">Risk Governance</h1>
                <p className="text-xs text-[var(--text-muted)] mt-1 font-mono">
                    Define risk limits and allocation rules for your portfolios
                </p>
            </div>
            <PolicyList />
        </div>
    );
}
