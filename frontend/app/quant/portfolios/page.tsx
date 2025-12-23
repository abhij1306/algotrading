import PortfolioLayout from "@/components/quant/PortfolioLayout";

export const metadata = {
    title: "Portfolio Research | SmartTrader",
    description: "Create and manage research portfolios",
};

export default function PortfoliosPage() {
    return (
        <div className="h-full w-full overflow-hidden">
            <PortfolioLayout />
        </div>
    );
}
