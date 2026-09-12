import { useCallback, useEffect, useRef, useState } from "react";
import { fetchBootstrap, POLL_INTERVAL_MS, STREAM_ENDPOINT, type BootstrapFetcher } from "./api";
import type { BootstrapDto, LiveStateEvent, SettingsEvent } from "./types";

export type TransportState = "REALTIME" | "RECONNECTING" | "POLLING FALLBACK";
export interface EventSourceLike {
  close(): void;
  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void;
  onopen: (() => void) | null;
  onerror: (() => void) | null;
}
export type EventSourceFactory = (url: string) => EventSourceLike;
export interface BootstrapPollingState {
  snapshot: BootstrapDto | null;
  lastSuccessfulRefresh: Date | null;
  offline: boolean;
  loading: boolean;
  transport: TransportState;
  reconnecting: boolean;
  resync(): Promise<void>;
}

const nativeEventSource: EventSourceFactory | undefined = typeof EventSource === "undefined"
  ? undefined : (url) => new EventSource(url) as EventSourceLike;

export function useBootstrap(
  client: BootstrapFetcher = fetchBootstrap,
  pollIntervalMs = POLL_INTERVAL_MS,
  eventSourceFactory: EventSourceFactory | undefined = nativeEventSource,
  fallbackDelayMs = 6_000,
  reconnectDelayMs = 2_000,
): BootstrapPollingState {
  const [snapshot, setSnapshot] = useState<BootstrapDto | null>(null);
  const snapshotRef = useRef<BootstrapDto | null>(null);
  const [lastSuccessfulRefresh, setLastSuccessfulRefresh] = useState<Date | null>(null);
  const [loading, setLoading] = useState(true);
  const [transport, setTransport] = useState<TransportState>("RECONNECTING");
  const [offline, setOffline] = useState(false);
  const [reconnecting, setReconnecting] = useState(eventSourceFactory !== undefined);
  const consecutiveFailures = useRef(0);
  const streamOpen = useRef(false);

  const contactSucceeded = useCallback(() => {
    consecutiveFailures.current = 0;
    setOffline(false);
  }, []);
  const contactFailed = useCallback(() => {
    // With a retained snapshot, one transient HTTP failure is insufficient
    // evidence that the backend is offline. An open SSE stream is positive
    // backend contact regardless of an auxiliary resync failure.
    if (streamOpen.current) return;
    consecutiveFailures.current += 1;
    if (!snapshotRef.current || consecutiveFailures.current >= 2) setOffline(true);
  }, []);

  const install = useCallback((next: BootstrapDto) => {
    snapshotRef.current = next;
    setSnapshot(next);
    setLastSuccessfulRefresh(new Date());
  }, []);
  const resync = useCallback(async () => {
    try {
      const started = snapshotRef.current;
      const next = await client();
      const current = snapshotRef.current;
      // A delayed HTTP snapshot must not undo newer SSE/HTTP state (and make
      // an active selected encounter appear withdrawn). Allow revision resets
      // after server restart when no newer state arrived during this request.
      if (!current || current === started ||
          (next.live_revision >= current.live_revision &&
           next.settings_revision >= current.settings_revision)) install(next);
      contactSucceeded();
    } catch (error) {
      contactFailed();
      throw error;
    }
  }, [client, contactFailed, contactSucceeded, install]);

  useEffect(() => {
    let cancelled = false;
    let source: EventSourceLike | undefined;
    let fallbackTimer: ReturnType<typeof setTimeout> | undefined;
    let pollTimer: ReturnType<typeof setTimeout> | undefined;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    let staleTimer: ReturnType<typeof setTimeout> | undefined;
    let fallbackActive = false;
    let connectionCount = 0;

    const pollFallback = async () => {
      if (cancelled || !fallbackActive) return;
      try {
        const started = snapshotRef.current;
        const next = await client();
        if (!cancelled && fallbackActive) {
          const current = snapshotRef.current;
          if (!current || current === started ||
              (next.live_revision >= current.live_revision &&
               next.settings_revision >= current.settings_revision)) install(next);
          contactSucceeded();
          setTransport("POLLING FALLBACK");
        }
      } catch {
        if (!cancelled && fallbackActive) contactFailed();
      }
      if (!cancelled && fallbackActive) pollTimer = setTimeout(pollFallback, pollIntervalMs);
    };
    const startFallback = () => {
      if (fallbackActive || cancelled) return;
      fallbackTimer = undefined;
      fallbackActive = true;
      setTransport("POLLING FALLBACK");
      void pollFallback();
    };
    const scheduleFallback = () => {
      if (fallbackTimer === undefined) fallbackTimer = setTimeout(startFallback, fallbackDelayMs);
    };
    const safeResync = async () => {
      try { await resync(); } catch { /* resync records transport failure */ }
    };
    const applyLive = (event: MessageEvent<string>) => {
      try {
        const update = JSON.parse(event.data) as LiveStateEvent;
        const current = snapshotRef.current;
        if (update.schema_version !== 1 || update.event !== "live_state" ||
            !Number.isInteger(update.live_revision) || !current) return;
        if (update.live_revision <= current.live_revision) return;
        if (update.live_revision !== current.live_revision + 1) { void safeResync(); return; }
        install({ ...current, ...update.payload, health: "ACTIVE",
          live_revision: update.live_revision });
        if (staleTimer !== undefined) clearTimeout(staleTimer);
        staleTimer = setTimeout(() => {
          const latest = snapshotRef.current;
          if (latest) {
            const stale = { ...latest, health: "STALE" as const };
            snapshotRef.current = stale;
            setSnapshot(stale);
          }
        }, 10_000);
      } catch { void safeResync(); }
    };
    const applySettings = (event: MessageEvent<string>) => {
      try {
        const update = JSON.parse(event.data) as SettingsEvent;
        const current = snapshotRef.current;
        if (update.schema_version !== 1 || update.event !== "settings" ||
            !Number.isInteger(update.settings_revision) || !current) return;
        if (update.settings_revision <= current.settings_revision) return;
        if (update.settings_revision !== current.settings_revision + 1) { void safeResync(); return; }
        install({ ...current, settings_revision: update.settings_revision,
          settings: update.payload.values,
          capabilities: update.payload.capabilities });
      } catch { void safeResync(); }
    };
    const connect = () => {
      if (cancelled || !eventSourceFactory || source) return;
      let candidate: EventSourceLike;
      try {
        candidate = eventSourceFactory(STREAM_ENDPOINT);
      } catch {
        scheduleReconnect();
        scheduleFallback();
        return;
      }
      source = candidate;
      connectionCount += 1;
      setReconnecting(true);
      setTransport("RECONNECTING");
      candidate.addEventListener("live_state", applyLive);
      candidate.addEventListener("settings", applySettings);
      candidate.onopen = () => {
        if (cancelled || source !== candidate) return;
        fallbackActive = false;
        if (pollTimer !== undefined) clearTimeout(pollTimer);
        pollTimer = undefined;
        if (fallbackTimer !== undefined) clearTimeout(fallbackTimer);
        fallbackTimer = undefined;
        streamOpen.current = true;
        contactSucceeded();
        setReconnecting(false);
        setTransport("REALTIME");
        if (connectionCount > 1) void safeResync();
      };
      candidate.onerror = () => {
        if (cancelled || source !== candidate) return;
        candidate.close();
        source = undefined;
        streamOpen.current = false;
        setReconnecting(true);
        setTransport("RECONNECTING");
        scheduleFallback();
        scheduleReconnect();
      };
    };
    const scheduleReconnect = () => {
      if (cancelled || reconnectTimer !== undefined || !eventSourceFactory) return;
      reconnectTimer = setTimeout(() => {
        reconnectTimer = undefined;
        connect();
      }, reconnectDelayMs);
    };
    const start = async () => {
      try { await resync(); } catch { /* resync records transport failure */ }
      finally { if (!cancelled) setLoading(false); }
      if (cancelled) return;
      if (!eventSourceFactory) { setReconnecting(false); startFallback(); return; }
      connect();
    };
    void start();
    return () => {
      cancelled = true;
      streamOpen.current = false;
      source?.close();
      if (fallbackTimer !== undefined) clearTimeout(fallbackTimer);
      if (pollTimer !== undefined) clearTimeout(pollTimer);
      if (reconnectTimer !== undefined) clearTimeout(reconnectTimer);
      if (staleTimer !== undefined) clearTimeout(staleTimer);
    };
  }, [contactFailed, contactSucceeded, eventSourceFactory, fallbackDelayMs, pollIntervalMs,
    reconnectDelayMs, resync, install]);

  return { snapshot, lastSuccessfulRefresh, offline, loading, transport, reconnecting, resync };
}
