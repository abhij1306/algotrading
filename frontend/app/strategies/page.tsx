import { PageContainer, Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui";
import { BacktestPanel } from "@/components/strategies/BacktestPanel";
import { PositionTable } from "@/components/strategies/PositionTable";
import { SignalChartCard } from "@/components/strategies/SignalChartCard";
import { SignalDetailCard } from "@/components/strategies/SignalDetailCard";
import { SignalTable } from "@/components/strategies/SignalTable";
import { StrategiesHeader } from "@/components/strategies/StrategiesHeader";
import { StrategyStatCards } from "@/components/strategies/StrategyStatCards";
import { latestError } from "@/components/strategies/strategy-view";
import { useStrategyActions } from "./use-strategy-actions";
import { useStrategyData } from "./use-strategy-data";

export default function StrategiesPage() {
  const data = useStrategyData();
  const actions = useStrategyActions(data.universe);
  const error = latestError([actions.actionError, ...data.queryErrors]);

  const onSelectSymbol = (symbol: string) => {
    data.selectSymbol(symbol);
    actions.clearError();
  };

  return (
    <PageContainer fullWidth>
      <div className="flex h-full w-full flex-col overflow-hidden bg-background">
        <StrategiesHeader
          status={data.status}
          showAll={data.showAll}
          busy={actions.busy}
          onToggleShowAll={() => {
            data.toggleShowAll();
            actions.clearError();
          }}
          onRunScan={actions.runScan}
          onToggleHalt={() => actions.toggleHalt(data.status?.halted ?? false)}
        />

        {error && <div className="bg-loss-bg px-4 py-2 text-sm text-loss">{error}</div>}

        <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1.5fr)_minmax(360px,0.9fr)] overflow-hidden">
          <div className="min-h-0 overflow-hidden border-r border-border">
            <StrategyStatCards
              universes={data.universes}
              universe={data.universe}
              status={data.status}
              signalCount={data.results.length}
              onChooseUniverse={(id) => {
                data.chooseUniverse(id);
                actions.clearError();
              }}
            />

            <Tabs defaultValue="forming" className="flex h-[calc(100%-88px)] flex-col">
              <TabsList className="mx-4 mt-3 grid grid-cols-4">
                <TabsTrigger value="forming">Forming {data.forming.length}</TabsTrigger>
                <TabsTrigger value="breakouts">Breakouts {data.breakouts.length}</TabsTrigger>
                <TabsTrigger value="positions">Positions {data.positions.length}</TabsTrigger>
                <TabsTrigger value="backtest">Backtest</TabsTrigger>
              </TabsList>

              <TabsContent value="forming" className="min-h-0 flex-1 overflow-auto px-4 pb-4">
                {data.loading ? (
                  <div className="py-4 text-sm text-foreground-secondary">
                    Loading scanner results...
                  </div>
                ) : null}
                <SignalTable
                  rows={data.forming}
                  onSelect={onSelectSymbol}
                  onQueueToggle={actions.toggleQueue}
                  busy={actions.busy}
                  selectedSymbol={data.signalSymbol}
                />
              </TabsContent>

              <TabsContent value="breakouts" className="min-h-0 flex-1 overflow-auto px-4 pb-4">
                <SignalTable
                  rows={data.breakouts}
                  onSelect={onSelectSymbol}
                  onQueueToggle={actions.toggleQueue}
                  busy={actions.busy}
                  selectedSymbol={data.signalSymbol}
                />
              </TabsContent>

              <TabsContent value="positions" className="min-h-0 flex-1 overflow-auto px-4 pb-4">
                <PositionTable
                  rows={data.positions}
                  onClose={actions.closePosition}
                  busy={actions.busy}
                />
              </TabsContent>

              <TabsContent value="backtest" className="min-h-0 flex-1 overflow-auto px-4 pb-4">
                <BacktestPanel
                  universe={data.universe}
                  form={actions.backtestForm}
                  result={actions.backtestResult}
                  runs={data.runs}
                  busy={actions.busy}
                  onEdit={actions.editBacktest}
                  onRun={actions.runBacktest}
                />
              </TabsContent>
            </Tabs>
          </div>

          <div className="flex min-h-0 flex-col bg-background-secondary">
            <SignalDetailCard signal={data.signalDetail?.signal ?? null} />
            <SignalChartCard data={data.signalDetail?.chart} />
          </div>
        </div>
      </div>
    </PageContainer>
  );
}
