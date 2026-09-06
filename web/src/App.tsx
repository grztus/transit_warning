import { useEffect, useRef, useState } from "react";
import { fetchHistory, mobileGpsClient as defaultMobileGpsClient, normalizeHistoryMaxSep, patchSettings, SettingsConflictError } from "./api";
import type { BootstrapFetcher, HistoryFetcher, MobileGpsClient, SettingsMutator } from "./api";
import type {
  BodyName,
  BodyStateDto,
  RecentEventDto,
  HistoryEventDto,
  TransitCandidateDto,
} from "./types";
import { useBootstrap } from "./useBootstrap";
import type { EventSourceFactory } from "./useBootstrap";
import "./styles.css";

interface AppProps {
  client?: BootstrapFetcher;
  pollIntervalMs?: number;
  eventSourceFactory?: EventSourceFactory;
  fallbackDelayMs?: number;
  reconnectDelayMs?: number;
  settingsMutator?: SettingsMutator;
  historyClient?: HistoryFetcher;
  gpsClient?: MobileGpsClient;
}

const value = (number: number | undefined, suffix = "") =>
  number === undefined ? "—" : `${number.toFixed(1)}${suffix}`;

const separation = (number: number | undefined) =>
  number === undefined ? "—" : `${number.toFixed(2)}°`;

const manualNumber = (text: string, minimum: number, maximum: number) => {
  const canonical = text.trim().replace(",", ".");
  if (!/^-?(?:\d+(?:\.\d*)?|\.\d+)$/.test(canonical)) return null;
  const number = Number(canonical);
  return Number.isFinite(number) && minimum <= number && number <= maximum
    ? number : null;
};

export function settingsCommandId() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  if (typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function") {
    const values = crypto.getRandomValues(new Uint32Array(4));
    return `web-${Array.from(values, (item) => item.toString(16).padStart(8, "0")).join("")}`;
  }
  return `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export function geolocationErrorMessage(error: Pick<GeolocationPositionError, "code">) {
  if (error.code === 1) return "Browser GPS permission was denied";
  if (error.code === 2) return "Browser GPS position is unavailable";
  if (error.code === 3) return "Browser GPS request timed out";
  return "Browser GPS acquisition failed";
}

const time = (timestamp: string | undefined) => {
  if (!timestamp) return "—";
  const parsed = new Date(timestamp);
  if (!Number.isFinite(parsed.getTime())) return timestamp;
  return new Intl.DateTimeFormat(undefined, {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        timeZone: "UTC",
        hour12: false,
      }).format(parsed) + " UTC";
};

const duration = (totalSeconds: number) => {
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
};

export function formatCountdown(timestamp: string | undefined, nowMs: number) {
  if (!timestamp) return null;
  const eventMs = Date.parse(timestamp);
  if (!Number.isFinite(eventMs)) return null;
  const seconds = Math.max(0, Math.floor((eventMs - nowMs) / 1000));
  return duration(seconds);
}

function useLiveClock() {
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, []);
  return nowMs;
}

const className = (value: string | undefined) =>
  (value || "neutral").toLowerCase().replace(/[^a-z0-9_-]/g, "-");

function EditionBadge() {
  return (
    <span className="edition-badge-wrap">
      <button type="button" className="edition-badge" aria-describedby="edition-tooltip">
        STANDARD
      </button>
      <span id="edition-tooltip" className="edition-tooltip" role="tooltip">
        <strong>Standard Version</strong>
        <span>Free operational dashboard.</span>
        <span>Want the advanced tools? Support the project and help fund further development.</span>
      </span>
    </span>
  );
}

function CandidateCard({ candidate, nowMs }: {
  candidate: TransitCandidateDto;
  nowMs: number;
}) {
  const state = candidate.state || "CANDIDATE";
  const countdown = formatCountdown(candidate.predicted_event_utc, nowMs);
  return (
    <article className="candidate-card">
      <div className="candidate-title">
        <div className="candidate-identity">
          {countdown && <strong className="countdown">{countdown}</strong>}
          <strong className="callsign">{candidate.callsign || candidate.icao || "NOCALL"}</strong>
          <span className="event-time">{time(candidate.predicted_event_utc)}</span>
        </div>
        <span className={`sep sep-${className(candidate.separation_class)}`}>
          SEP {separation(candidate.separation_deg)}
        </span>
      </div>
      <div className="candidate-summary">
        <span className={`state-badge state-${className(state)}`}>{state}</span>
        {candidate.transit_distance_km != null &&
          <span className="candidate-transit-distance">
            Transit distance {value(candidate.transit_distance_km, " km")}
          </span>}
      </div>
      <dl className="compact-grid">
        <dt>Geometry</dt><dd>{candidate.prediction_geometry || "—"}</dd>
        <dt>Encounter</dt><dd className="encounter-id">{candidate.encounter_id || "—"}</dd>
      </dl>
    </article>
  );
}

function BodyPanel({ name, state, nowMs }: {
  name: BodyName;
  state: BodyStateDto;
  nowMs: number;
}) {
  const position = state.current_position;
  return (
    <section className="panel body-panel">
      <header>
        <h2 className={`body-heading body-${name.toLowerCase()}`}>
          <span aria-hidden="true">{name === "SUN" ? "☀" : "☾"}</span> {name}
        </h2>
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
              nowMs={nowMs}
            />
          ))}
        </div>
      ) : <p className="empty">No current candidates</p>}
    </section>
  );
}

function EventCard({ event, showIcao = false }: {
  event: RecentEventDto | HistoryEventDto;
  showIcao?: boolean;
}) {
  const outcome = event.outcome || event.state || "—";
  const icao = "icao_hex" in event ? event.icao_hex || event.icao : event.icao;
  return (
    <li className={`event-row event-${className(outcome)}`}>
      <span className="event-identity">
        <strong>{event.callsign || icao || "NOCALL"}</strong>
        {showIcao && event.callsign && icao &&
          <span className="event-icao">{icao.toUpperCase()}</span>}
      </span>
      <span className={`event-body body-${className(event.body)}`}>{event.body || "—"}</span>
      <span className={`state-badge state-${className(outcome)}`}>{outcome}</span>
      <span className={`sep sep-${className(event.separation_class)}`}>
        SEP {separation(event.final_separation_deg ?? event.separation_deg)}
      </span>
      <span className="event-time">{time(event.predicted_event_utc)}</span>
      {event.transit_distance_km != null && <span className="event-distance">
        {value(event.transit_distance_km, " km")}
      </span>}
    </li>
  );
}

export default function App({ client, pollIntervalMs, eventSourceFactory, fallbackDelayMs,
  reconnectDelayMs, settingsMutator = patchSettings, historyClient = fetchHistory,
  gpsClient = defaultMobileGpsClient }: AppProps) {
  const state = useBootstrap(client, pollIntervalMs, eventSourceFactory, fallbackDelayMs,
    reconnectDelayMs);
  const nowMs = useLiveClock();
  const snapshot = state.snapshot;
  const health = state.offline ? "OFFLINE" : snapshot?.health || "STALE";
  const [pending, setPending] = useState<string | null>(null);
  const [settingsMessage, setSettingsMessage] = useState<string | null>(null);
  const [manualDraft, setManualDraft] = useState({ lat: "", lon: "", elevation: "" });
  const [manualError, setManualError] = useState<string | null>(null);
  const [manualEditorOpen, setManualEditorOpen] = useState(false);
  const [gpsAvailable, setGpsAvailable] = useState<boolean | null>(null);
  const [gpsRunning, setGpsRunning] = useState(false);
  const [gpsMessage, setGpsMessage] = useState<string | null>(null);
  const gpsWatchId = useRef<number | null>(null);
  const gpsActive = useRef(false);
  const [view, setView] = useState<"LIVE" | "HISTORY">("LIVE");
  const [historyDate, setHistoryDate] = useState("");
  const [historyFromDate, setHistoryFromDate] = useState("");
  const [historyToDate, setHistoryToDate] = useState("");
  const [historyCallsign, setHistoryCallsign] = useState("");
  const [historyBody, setHistoryBody] = useState<"ALL" | "SUN" | "MOON">("ALL");
  const [historyMaxSep, setHistoryMaxSep] = useState("");
  const [historyRecords, setHistoryRecords] = useState<HistoryEventDto[]>([]);
  const [historyNextOffset, setHistoryNextOffset] = useState<number | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState(false);
  const historyRequest = useRef(0);
  useEffect(() => {
    let active = true;
    void gpsClient.status().then((diagnostics) => {
      if (active) setGpsAvailable(diagnostics.available);
    }).catch(() => {
      if (!active) return;
      setGpsAvailable(false);
    });
    return () => {
      active = false;
      gpsActive.current = false;
      if (gpsWatchId.current !== null && navigator.geolocation) {
        navigator.geolocation.clearWatch(gpsWatchId.current);
      }
      gpsWatchId.current = null;
    };
  }, [gpsClient]);
  useEffect(() => {
    if (!gpsRunning) return;
    const timer = window.setInterval(() => void state.resync(), 3_000);
    return () => window.clearInterval(timer);
  }, [gpsRunning, state.resync]);
  const loadHistory = async (append = false) => {
    const request = ++historyRequest.current;
    setHistoryLoading(true);
    setHistoryError(false);
    if (!append) setHistoryRecords([]);
    try {
      const maxSepDeg = normalizeHistoryMaxSep(historyMaxSep);
      const page = await historyClient({ date: historyDate || undefined,
        fromDate: historyFromDate || undefined, toDate: historyToDate || undefined,
        callsign: historyCallsign.trim() || undefined,
        body: historyBody, maxSepDeg,
        offset: append ? historyNextOffset ?? 0 : 0, limit: 25 });
      if (request !== historyRequest.current) return;
      setHistoryRecords((current) => append ? current.concat(page.records) : page.records);
      setHistoryNextOffset(page.has_more ? page.next_offset : null);
    } catch {
      if (request !== historyRequest.current) return;
      setHistoryRecords([]);
      setHistoryNextOffset(null);
      setHistoryError(true);
    } finally {
      if (request === historyRequest.current) setHistoryLoading(false);
    }
  };
  useEffect(() => {
    if (view === "HISTORY") void loadHistory();
  // Filters intentionally trigger the same immediate refresh as the legacy controls.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, historyDate, historyFromDate, historyToDate, historyCallsign,
    historyBody, historyMaxSep, historyClient]);
  useEffect(() => {
    if (!snapshot || pending === "manual-position") return;
    const manual = snapshot.settings.observer;
    setManualDraft(manual.manual_position_saved
      ? { lat: String(manual.manual_lat_deg), lon: String(manual.manual_lon_deg),
        elevation: String(manual.manual_elevation_amsl_m) }
      : { lat: "", lon: "", elevation: "" });
  }, [snapshot?.settings.observer.manual_lat_deg,
    snapshot?.settings.observer.manual_lon_deg,
    snapshot?.settings.observer.manual_elevation_amsl_m,
    snapshot?.settings.observer.manual_position_saved, pending]);
  const changeSetting = async (key: string, changes: Parameters<SettingsMutator>[0]["changes"]) => {
    if (!snapshot || pending) return;
    setPending(key);
    setSettingsMessage(null);
    try {
      await settingsMutator({ expected_revision: snapshot.settings_revision,
        command_id: settingsCommandId(), changes });
      await state.resync();
      return true;
    } catch (error) {
      if (error instanceof SettingsConflictError) {
        setSettingsMessage("Setting changed on another client");
        try { await state.resync(); } catch { /* transport status reports failure */ }
      } else {
        setSettingsMessage(error instanceof Error ? error.message : "Setting change failed");
      }
    } finally {
      setPending(null);
    }
    return false;
  };
  const saveManualPosition = async () => {
    const lat = manualNumber(manualDraft.lat, -90, 90);
    const lon = manualNumber(manualDraft.lon, -180, 180);
    const elevation = manualNumber(manualDraft.elevation, -500, 10000);
    if (lat === null || lon === null || elevation === null) {
      setManualError("Enter valid LAT, LON, and elevation AMSL values");
      return;
    }
    setManualError(null);
    const saved = await changeSetting("manual-position", { observer: {
      requested_mode: "MANUAL", manual_lat_deg: lat,
      manual_lon_deg: lon, manual_elevation_amsl_m: elevation } });
    if (saved) setManualEditorOpen(false);
  };

  const selectObserverMode = (mode: "STATIC" | "MOBILE" | "MANUAL") => {
    if (mode === "MANUAL" && !snapshot?.settings.observer.manual_position_saved) {
      setManualEditorOpen(true);
      setManualError(null);
      setSettingsMessage(null);
      return;
    }
    void changeSetting("observer-mode", { observer: { requested_mode: mode } });
  };

  const startGps = () => {
    if (gpsRunning || gpsAvailable !== true) return;
    if (!navigator.geolocation) {
      setGpsMessage("Browser GPS is unavailable; use HTTPS or a supported browser");
      return;
    }
    gpsActive.current = true;
    setGpsRunning(true);
    setGpsMessage("Waiting for browser GPS position");
    try {
      gpsWatchId.current = navigator.geolocation.watchPosition(
        (position) => {
          if (!gpsActive.current) return;
          const coordinates = position.coords;
          void gpsClient.update({
            latitude: coordinates.latitude,
            longitude: coordinates.longitude,
            accuracy: coordinates.accuracy,
            altitude: coordinates.altitude,
            altitudeAccuracy: coordinates.altitudeAccuracy,
            timestamp: position.timestamp,
          }).then(() => {
            if (!gpsActive.current) return;
            setGpsMessage(null);
            void state.resync();
          }).catch((error) => {
            if (gpsActive.current) setGpsMessage(
              error instanceof Error ? error.message : "Mobile GPS update failed");
          });
        },
        (error) => {
          if (gpsActive.current) setGpsMessage(geolocationErrorMessage(error));
        },
        { enableHighAccuracy: true, timeout: 10_000, maximumAge: 0 },
      );
    } catch {
      gpsActive.current = false;
      setGpsRunning(false);
      setGpsMessage("Browser GPS is unavailable; use HTTPS or a supported browser");
    }
  };

  const stopGps = async () => {
    gpsActive.current = false;
    if (gpsWatchId.current !== null && navigator.geolocation) {
      navigator.geolocation.clearWatch(gpsWatchId.current);
    }
    gpsWatchId.current = null;
    setGpsRunning(false);
    setGpsMessage(null);
    try {
      await gpsClient.clear();
      await state.resync();
    } catch (error) {
      setGpsMessage(error instanceof Error ? error.message : "Could not stop Mobile GPS");
    }
  };

  return (
    <main>
      <header className="app-header">
        <div>
          <p className="eyebrow brand-line"><span>Transit Warning</span><EditionBadge /></p>
          <nav className="view-tabs" aria-label="Dashboard view">
            {(["LIVE", "HISTORY"] as const).map((name) => (
              <button key={name} type="button" aria-pressed={view === name}
                className={view === name ? "active" : undefined}
                onClick={() => setView(name)}>{name}</button>
            ))}
          </nav>
        </div>
        <div className={`health health-${health.toLowerCase()}`} role="status">
          <span className="health-dot" />{health}
        </div>
        <div className={`transport transport-${state.transport.toLowerCase().replaceAll(" ", "-")}`}>
          {state.transport}
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

          {view === "LIVE" ? <>
            <section className="predictions-section" aria-labelledby="predictions-title">
              <header className="predictions-header">
                <h2 id="predictions-title">TRANSIT PREDICTIONS</h2>
              </header>
              <div className="body-grid">
                <BodyPanel name="SUN" state={snapshot.bodies.sun} nowMs={nowMs} />
                <BodyPanel name="MOON" state={snapshot.bodies.moon} nowMs={nowMs} />
              </div>
            </section>

          <section className="panel controls-panel">
            <header><h2>Controls</h2><span className="controls-meta">
              API {snapshot.capabilities.runtime_settings ? "available" : "unavailable"} · rev {snapshot.settings_revision}
            </span></header>
            <div className="control-group observer-control-group">
              <strong>Observer</strong>
              <div className="control-row" aria-label="Observer mode">
              {(["STATIC", "MOBILE", "MANUAL"] as const).map((mode) => (
                <button key={mode} type="button"
                  aria-pressed={snapshot.settings.observer.requested_mode === mode}
                  disabled={pending !== null}
                  onClick={() => selectObserverMode(mode)}>
                  {pending === "observer-mode" ? "CHANGING…" : mode}
                </button>
              ))}
              </div>
              <span className="fallback-control-group">
                <span className="control-label">Fallback</span>
                <button type="button" aria-pressed={snapshot.settings.observer.fallback_enabled}
                  aria-label="Observer fallback"
                  disabled={pending !== null}
                  onClick={() => void changeSetting("fallback", { observer: {
                    fallback_enabled: !snapshot.settings.observer.fallback_enabled } })}>
                  {pending === "fallback" ? "CHANGING…" : snapshot.settings.observer.fallback_enabled ? "ON" : "OFF"}
                </button>
              </span>
              {snapshot.settings.observer.requested_mode === "MOBILE" && !gpsRunning &&
                <button type="button" className="gps-control" disabled={gpsAvailable !== true}
                  onClick={startGps}>Start GPS</button>}
              {gpsRunning && <button type="button" className="gps-control"
                onClick={() => void stopGps()}>Stop GPS</button>}
            </div>
            {(snapshot.settings.observer.requested_mode === "MANUAL" || manualEditorOpen) &&
              <div className="manual-observer" aria-label="Manual observer position">
                <label>LAT <input aria-label="Manual latitude" inputMode="decimal"
                  value={manualDraft.lat} onChange={(event) => setManualDraft({
                    ...manualDraft, lat: event.target.value })} /></label>
                <label>LON <input aria-label="Manual longitude" inputMode="decimal"
                  value={manualDraft.lon} onChange={(event) => setManualDraft({
                    ...manualDraft, lon: event.target.value })} /></label>
                <label>ELEV AMSL <input aria-label="Manual elevation AMSL" inputMode="decimal"
                  value={manualDraft.elevation} onChange={(event) => setManualDraft({
                    ...manualDraft, elevation: event.target.value })} /></label>
                <button type="button" disabled={pending !== null}
                  onClick={saveManualPosition}>SAVE</button>
              </div>}
            {manualError && <p className="settings-message" role="alert">{manualError}</p>}
            {gpsMessage && <p className="gps-message" role="alert">{gpsMessage}</p>}
            <p className="observer-meta">
              Requested {snapshot.observer.requested_mode || "—"} · Effective {snapshot.observer.effective_source || "—"}
              {snapshot.settings.observer.requested_mode === "MOBILE" && <>
                {" · "}GPS {snapshot.observer.gps_health || "—"}
                {" · "}Age {value(snapshot.observer.mobile_age_seconds ?? undefined, " s")}
                {" · "}Accuracy {value(snapshot.observer.mobile_accuracy_m ?? undefined, " m")}
              </>}
              {snapshot.observer.fallback_active && " · Fallback active"}
              {snapshot.observer.effective_elevation_m != null &&
                <> · Elevation {value(snapshot.observer.effective_elevation_m, " m AMSL")}</>}
            </p>
            {snapshot.settings.observer.requested_mode === "MOBILE" &&
              (snapshot.observer.fallback_active || !["MOBILE", "MOBILE_FRESH"].includes(
                snapshot.observer.effective_source || "")) && (
              <p className="degraded" role="status">MOBILE requested · effective {snapshot.observer.effective_source || "unavailable"}</p>
            )}
            <div className="control-group telegram-control-group">
              <strong>Telegram</strong>
            {(["sun", "moon"] as const).map((body) => (
              <div className="inline-toggle" key={body}>
                <span>{body.toUpperCase()}</span>
                <button type="button" aria-pressed={snapshot.settings.telegram[`${body}_enabled`]}
                  aria-label={`Telegram ${body.toUpperCase()}`}
                  disabled={pending !== null}
                  onClick={() => void changeSetting(`telegram-${body}`, { telegram: {
                    [`${body}_enabled`]: !snapshot.settings.telegram[`${body}_enabled`] } })}>
                  {pending === `telegram-${body}` ? "CHANGING…" :
                    snapshot.settings.telegram[`${body}_enabled`] ? "ON" : "OFF"}
                </button>
              </div>
            ))}
            </div>
            {settingsMessage && <p className="settings-message" role="alert">{settingsMessage}</p>}
          </section>
          <section className="panel recent-panel">
            <header><h2>Recent events</h2></header>
            {snapshot.recent_events.length ? (
              <ul className="event-list">
                {snapshot.recent_events.map((event, index) => (
                  <EventCard key={event.event_id || event.encounter_id || index} event={event} />
                ))}
              </ul>
            ) : <p className="empty">No recent events</p>}
          </section>
          </> :
          <section className="panel history-panel">
            <header><h2>History</h2></header>
            <div className="history-controls">
              <label>Date <input type="date" aria-label="UTC date" value={historyDate}
                onChange={(event) => setHistoryDate(event.target.value)} /></label>
              <label>From date <input type="date" aria-label="From date" value={historyFromDate}
                onChange={(event) => setHistoryFromDate(event.target.value)} /></label>
              <label>To date <input type="date" aria-label="To date" value={historyToDate}
                onChange={(event) => setHistoryToDate(event.target.value)} /></label>
              <label>Callsign <input type="search" aria-label="Callsign filter"
                placeholder="Callsign" value={historyCallsign}
                onChange={(event) => setHistoryCallsign(event.target.value)} /></label>
              <label>Body <select aria-label="Celestial body" value={historyBody}
                onChange={(event) => setHistoryBody(event.target.value as "ALL" | "SUN" | "MOON")}>
                <option value="ALL">ALL</option><option value="SUN">SUN</option>
                <option value="MOON">MOON</option>
              </select></label>
              <label>SEP ≤ <span className="sep-input"><input type="text" pattern="[0-9]+([.,][0-9]+)?"
                inputMode="decimal" aria-label="Maximum final separation" value={historyMaxSep}
                onChange={(event) => setHistoryMaxSep(event.target.value)} /><span>°</span></span></label>
            </div>
            {historyLoading ? <p className="empty" role="status">Loading history…</p> :
              historyError ? <p className="history-error" role="alert">History request failed.</p> :
              historyRecords.length ? (
              <ul className="event-list">
                {historyRecords.map((event, index) => (
                  <EventCard key={event.event_id || index} event={event} showIcao />
                ))}
              </ul>
            ) : <p className="empty">No matching events</p>}
            <div className="history-actions">
              {!historyLoading && !historyError && historyNextOffset !== null &&
                <button type="button" className="load-more"
                  onClick={() => void loadHistory(true)}>Load more</button>}
              <form method="get" action="/api/history/export.csv">
                <input type="hidden" name="date" value={historyDate} />
                <input type="hidden" name="from_date" value={historyFromDate} />
                <input type="hidden" name="to_date" value={historyToDate} />
                <input type="hidden" name="callsign" value={historyCallsign.trim()} />
                <input type="hidden" name="body" value={historyBody} />
                <input type="hidden" name="max_sep_deg"
                  value={historyMaxSep.trim().replace(",", ".")} />
                <button type="submit" className="export-csv">Export CSV</button>
              </form>
            </div>
          </section>
          }
        </>
      )}
      <footer className="app-footer">
        Transit Warning · Grzegorz Tuszyński · <a href="https://github.com/grztus/transit_warning"
          target="_blank" rel="noopener noreferrer">GitHub</a>
      </footer>
    </main>
  );
}
