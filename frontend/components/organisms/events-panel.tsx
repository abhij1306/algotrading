import { noRows, QuerySection } from "@/components/molecules/query-section";
import { useHatEvents } from "@/lib/queries/hat";
import { formatIstDateTime } from "@/lib/time";

/** The latest audit events: when, what and who. */
export function EventsPanel() {
  const events = useHatEvents();
  return (
    <QuerySection title="Recent events" query={events} isEmpty={noRows} empty="No events.">
      {(data) => (
        <ul className="space-y-1 text-sm">
          {data.map((event) => (
            <li key={event.id} className="flex gap-3">
              <span className="font-mono text-foreground-muted">
                {formatIstDateTime(event.occurred_at)}
              </span>
              <span>{event.event_type}</span>
              <span className="text-foreground-muted">{event.actor}</span>
            </li>
          ))}
        </ul>
      )}
    </QuerySection>
  );
}
