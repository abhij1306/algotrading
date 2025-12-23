import { StrategyLibrary } from "@/components/quant/StrategyLibrary";

export const metadata = {
    title: "Strategy Library | SmartTrader",
    description: "Browse and manage quantitative trading strategies",
};

import { PageContainer } from "@/components/ui/PageContainer";

export default function StrategiesPage() {
    return (
        <PageContainer>
            <StrategyLibrary />
        </PageContainer>
    );
}
