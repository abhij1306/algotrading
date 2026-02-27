import { Card, PageContainer } from "@/components/ui";

export default function Loading() {
  const filterSkeletonKeys = ['filter-a', 'filter-b', 'filter-c', 'filter-d', 'filter-e'];
  const rowSkeletonKeys = Array.from({ length: 10 }, (_, idx) => `row-${idx + 1}`);

  return (
    <PageContainer>
      <div className="space-y-6">
        {/* Filters Bar Skeleton */}
        <div className="flex gap-4 overflow-x-auto pb-2">
          {filterSkeletonKeys.map((key) => (
            <div
              key={key}
              className="h-10 w-32 bg-white/5 rounded-full animate-pulse border border-white/5 flex-shrink-0"
            />
          ))}
        </div>

        {/* Table Skeleton */}
        <Card variant="glass" className="p-0 overflow-hidden">
          <div className="space-y-1">
            {/* Header Row */}
            <div className="h-12 bg-white/5 border-b border-white/5" />

            {/* Data Rows */}
            {rowSkeletonKeys.map((key) => (
              <div key={key} className="h-12 bg-transparent border-b border-white/5 animate-pulse" />
            ))}
          </div>
        </Card>
      </div>
    </PageContainer>
  );
}
