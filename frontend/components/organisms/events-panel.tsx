import { useState } from "react";
import { DetailList, Drawer } from "@/components/molecules/drawer";
import { LabelledValue } from "@/components/molecules/labelled-value";
import { noRows, QuerySection } from "@/components/molecules/query-section";
import { useHatEvents, type HatEvents } from "@/lib/queries/hat";
import { formatIstDateTime } from "@/lib/time";

type Event = HatEvents[number];

function EventDetails({ event, onClose }: Readonly<{ event: Event; onClose: () => void }>) {
  return (
    <Drawer title={event.event_type} subtitle={`#${event.id} by ${event.actor}`} onClose={onClose}>
      <DetailList>
        <LabelledValue label="Occurred">{formatIstDateTime(event.occurred_at)} IST</LabelledValue>
        <LabelledValue label="Recorded">{formatIstDateTime(event.recorded_at)} IST</LabelledValue>
        <LabelledValue label="Aggregate" className="col-span-2">
          {event.aggregate_type}{" "}
          <span className="font-mono text-xs text-foreground-muted">{event.aggregate_id}</span>
        </LabelledValue>
      </DetailList>
      <div>
        <p className="mb-2 text-xs text-foreground-muted">Payload</p>
        <pre className="whitespace-pre-wrap break-words rounded-md bg-subtle p-3 font-mono text-xs">
          {JSON.stringify(event.payload, null, 2)}
        </pre>
      </div>
    </Drawer>
  );
}

/** The latest audit events: when, what and who; an event opens its payload. */
export function EventsPanel({ className }: Readonly<{ className?: string }>) {
  const events = useHatEvents();
  const [openId, setOpenId] = useState<number | null>(null);
  const open = events.data?.find((event) => event.id === openId);
  return (
    <QuerySection
      title="Recent events"
      query={events}
      isEmpty={noRows}
      empty="No events."
      meta={(data) => `latest ${data.length}`}
      className={className}
    >
      {(data) => (
        <>
          <ul className="text-sm">
            {data.map((event) => (
              <li key={event.id}>
                <button
                  type="button"
                  onClick={() => setOpenId(event.id)}
                  className="flex w-full items-baseline gap-4 rounded py-1.5 text-left hover:bg-hover focus-visible:outline-2 focus-visible:outline-accent"
                >
                  <span className="shrink-0 text-xs tabular-nums text-foreground-muted">
                    {formatIstDateTime(event.occurred_at)}
                  </span>
                  <span className="min-w-0 flex-1 truncate">{event.event_type}</span>
                  <span className="shrink-0 text-foreground-muted">{event.actor}</span>
                </button>
              </li>
            ))}
          </ul>
          {open ? <EventDetails event={open} onClose={() => setOpenId(null)} /> : null}
        </>
      )}
    </QuerySection>
  );
}
