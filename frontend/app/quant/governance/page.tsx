import PolicyList from "@/components/quant/PolicyList";

export const metadata = {
    title: "Risk Governance | SmartTrader",
    description: "Configure portfolio risk policies and governance rules",
};

export default function GovernancePage() {
    return (
        <div className="h-full w-full p-6 overflow-auto">
            <div className="mb-6">
                <h1 className="text-xl font-bold text-white tracking-tight">Risk Governance</h1>
                <p className="text-gray-400 mt-1 text-xs font-mono">
                    Define risk limits and allocation rules for your portfolios
                </p>
            </div>
            <PolicyList />
        </div>
    );
}
