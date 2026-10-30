import { AllocationsPanel } from "@/components/organisms/allocations-panel";
import { BooksPanel } from "@/components/organisms/books-panel";
import { BotsPanel } from "@/components/organisms/bots-panel";
import { EventsPanel } from "@/components/organisms/events-panel";
import { FreshnessLine } from "@/components/organisms/freshness-line";
import { PositionsPanel } from "@/components/organisms/positions-panel";
import { SignalsPanel } from "@/components/organisms/signals-panel";
import { StaleBanner } from "@/components/organisms/stale-banner";
import { StatusPanel } from "@/components/organisms/status-panel";
import { WatchlistPanel } from "@/components/organisms/watchlist-panel";
import { PageLayout } from "@/components/templates/page-layout";

/** The read-only HAT dashboard; each panel polls its own route and marks itself stale. */
export default function HatPage() {
  return (
    <PageLayout
      title="Hire a Trader"
      description="Read-only. Kill, re-arm, mode and flatten are Telegram commands."
      aside={<FreshnessLine />}
    >
      <StaleBanner />
      <div className="grid gap-x-16 gap-y-14 lg:grid-cols-12">
        <StatusPanel className="lg:col-span-12" />
        <BotsPanel className="lg:col-span-12" />
        <BooksPanel className="lg:col-span-12" />
        <PositionsPanel className="lg:col-span-7" />
        <AllocationsPanel className="lg:col-span-5" />
        <SignalsPanel className="lg:col-span-12" />
        <WatchlistPanel className="lg:col-span-5" />
        <EventsPanel className="lg:col-span-7" />
      </div>
    </PageLayout>
  );
}
