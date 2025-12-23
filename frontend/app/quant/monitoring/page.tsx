import LiveDashboard from "@/components/quant/LiveDashboard";
import { PageContainer } from "@/components/ui/PageContainer";

export const metadata = {
    title: "System Monitor | SmarTrader",
    description: "Real-time portfolio monitoring and system health dashboard",
};

export default function MonitoringPage() {
    return (
        <PageContainer>
            <LiveDashboard />
        </PageContainer>
    );
}
