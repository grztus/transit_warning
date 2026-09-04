import type { BootstrapFetcher } from "./api";
import type {
  BodyName,
  BodyStateDto,
  RecentEventDto,
  TransitCandidateDto,
} from "./types";
import { useBootstrap } from "./useBootstrap";
import "./styles.css";

interface AppProps {
  client?: BootstrapFetcher;
  pollIntervalMs?: number;
}

const value = (number: number | undefined, suffix = "") =>
  number === undefined ? "—" : `${number.toFixed(1)}${suffix}`;

const time = (timestamp: string | undefined) =>
  timestamp
    ? new Intl.DateTimeFormat(undefined, {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        timeZone: "UTC",
        hour12: false,
      }).format(new Date(timestamp)) + " UTC"
    : "—";

function CandidateCard({ candidate }: { candidate: TransitCandidateDto }) {
  return (
    <article className="candidate-card">
      <div className="candidate-title">
        <strong>{candidate.callsign || candidate.icao || "NOCALL"}</strong>
        <span className={`sep ${candidate.separation_class || ""}`}>
          SEP {value(candidate.separation_deg, "°")}
        </span>
      </div>
      <dl className="compact-grid">
        <dt>Event</dt><dd>{time(candidate.predicted_event_utc)}</dd>
        <dt>State</dt><dd>{candidate.state || "—"}</dd>
        <dt>Geometry</dt><dd>{candidate.prediction_geometry || "—"}</dd>
        <dt>Encounter</dt><dd>{candidate.encounter_id || "—"}</dd>
      </dl>
    </article>
  );
}

function BodyPanel({ name, state }: { name: BodyName; state: BodyStateDto }) {
  const position = state.current_position;
  return (
    <section className="panel body-panel">
      <header>
        <h2>{name}</h2>
        <span className="body-position">
          ALT {value(position?.altitude_deg, "°")} · AZ {value(position?.azimuth_deg, "°")}
        </span>
      </header>
      {state.candidates.length ? (
        <div className="candidate-list">
          {state.candidates.map((candidate, index) => (
            <CandidateCard
              key={candidate.encounter_id || `${candidate.icao}-${index}`}
              candidate={candidate}
            />
          ))}
        </div>
      ) : <p className="empty">No current candidates</p>}
    </section>
  );
}

function EventCard({ event }: { event: RecentEventDto }) {
  return (
    <li>
      <strong>{event.callsign || event.icao || "NOCALL"}</strong>
      <span>{event.body || "—"}</span>
      <span>{event.outcome || event.state || "—"}</span>
      <span>SEP {value(event.final_separation_deg ?? event.separation_deg, "°")}</span>
    </li>
  );
}

export default function App({ client, pollIntervalMs }: AppProps) {
  const state = useBootstrap(client, pollIntervalMs);
  const snapshot = state.snapshot;
  const health = state.offline ? "OFFLINE" : snapshot?.health || "STALE";

  return (
    <main>
      <header className="app-header">
        <div>
          <p className="eyebrow">Transit Warning</p>
          <h1>LIVE</h1>
        </div>
        <div className={`health health-${health.toLowerCase()}`} role="status">
          <span className="health-dot" />{health}
        </div>
      </header>

      {state.offline && snapshot && (
        <p className="offline-banner">Connection failed — showing the last valid snapshot.</p>
      )}
      {!snapshot ? (
        <section className="panel empty-state">
          {state.loading ? "Loading live state…" : "Backend unavailable. Retrying automatically."}
        </section>
      ) : (
        <>
          <section className="status-strip panel">
            <div><span>Backend state</span><strong>{snapshot.health}</strong></div>
            <div><span>Generated</span><strong>{time(snapshot.generated_at_utc || undefined)}</strong></div>
            <div><span>Last refresh</span><strong>{state.lastSuccessfulRefresh?.toLocaleTimeString() || "—"}</strong></div>
          </section>

          <div className="body-grid">
            <BodyPanel name="SUN" state={snapshot.bodies.sun} />
            <BodyPanel name="MOON" state={snapshot.bodies.moon} />
          </div>

          <section className="panel">
            <header><h2>Observer</h2></header>
            <dl className="compact-grid observer-grid">
              <dt>Requested</dt><dd>{snapshot.observer.requested_mode || "—"}</dd>
              <dt>Effective</dt><dd>{snapshot.observer.effective_source || "—"}</dd>
              <dt>GPS</dt><dd>{snapshot.observer.gps_health || "—"}</dd>
              <dt>Age</dt><dd>{value(snapshot.observer.mobile_age_seconds ?? undefined, " s")}</dd>
              <dt>Accuracy</dt><dd>{value(snapshot.observer.mobile_accuracy_m ?? undefined, " m")}</dd>
              <dt>Fallback</dt><dd>{snapshot.observer.fallback_active ? "ACTIVE" : "OFF"}</dd>
            </dl>
          </section>

          <section className="panel">
            <header><h2>Recent events</h2></header>
            {snapshot.recent_events.length ? (
              <ul className="event-list">
                {snapshot.recent_events.map((event, index) => (
                  <EventCard key={event.event_id || event.encounter_id || index} event={event} />
                ))}
              </ul>
            ) : <p className="empty">No recent events</p>}
          </section>

          <section className="panel read-only">
            <header><h2>Capabilities</h2><span>Read-only</span></header>
            <p>Runtime settings API: {snapshot.capabilities.runtime_settings ? "available" : "unavailable"}</p>
            <p>Settings revision: {snapshot.settings_revision}</p>
          </section>
        </>
      )}
    </main>
  );
}
