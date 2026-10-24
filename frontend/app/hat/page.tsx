import { AllocationsPanel } from "@/components/organisms/allocations-panel";
import { BotsPanel } from "@/components/organisms/bots-panel";
import { EventsPanel } from "@/components/organisms/events-panel";
import { PositionsPanel } from "@/components/organisms/positions-panel";
import { SignalsPanel } from "@/components/organisms/signals-panel";
import { StatusPanel } from "@/components/organisms/status-panel";
import { WatchlistPanel } from "@/components/organisms/watchlist-panel";
import { PageLayout } from "@/components/templates/page-layout";

/** The read-only HAT dashboard; each panel polls its own route. */
export default function HatPage() {
  return (
    <PageLayout
      title="Hire a Trader"
      description="Read-only. Kill, re-arm, mode and flatten are Telegram commands."
    >
      <div className="grid gap-4 lg:grid-cols-2">
        <StatusPanel />
        <BotsPanel />
        <AllocationsPanel />
        <WatchlistPanel />
        <PositionsPanel />
        <SignalsPanel />
        <EventsPanel />
      </div>
    </PageLayout>
  );
}
