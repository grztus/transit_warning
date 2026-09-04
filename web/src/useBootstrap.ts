import { useCallback, useEffect, useRef, useState } from "react";
import { fetchBootstrap, POLL_INTERVAL_MS, STREAM_ENDPOINT, type BootstrapFetcher } from "./api";
import type { BootstrapDto, LiveStateEvent, SettingsEvent } from "./types";

export type TransportState = "REALTIME" | "RECONNECTING" | "POLLING FALLBACK" | "OFFLINE";
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
}

const nativeEventSource: EventSourceFactory | undefined = typeof EventSource === "undefined"
  ? undefined : (url) => new EventSource(url) as EventSourceLike;

export function useBootstrap(
  client: BootstrapFetcher = fetchBootstrap,
  pollIntervalMs = POLL_INTERVAL_MS,
  eventSourceFactory: EventSourceFactory | undefined = nativeEventSource,
  fallbackDelayMs = 6_000,
): BootstrapPollingState {
  const [snapshot, setSnapshot] = useState<BootstrapDto | null>(null);
  const snapshotRef = useRef<BootstrapDto | null>(null);
  const [lastSuccessfulRefresh, setLastSuccessfulRefresh] = useState<Date | null>(null);
  const [loading, setLoading] = useState(true);
  const [transport, setTransport] = useState<TransportState>("RECONNECTING");

  const install = useCallback((next: BootstrapDto) => {
    snapshotRef.current = next;
    setSnapshot(next);
    setLastSuccessfulRefresh(new Date());
  }, []);
  const resync = useCallback(async () => {
    const next = await client();
    install(next);
  }, [client, install]);

  useEffect(() => {
    let cancelled = false;
    let source: EventSourceLike | undefined;
    let fallbackTimer: ReturnType<typeof setTimeout> | undefined;
    let pollTimer: ReturnType<typeof setTimeout> | undefined;
    let staleTimer: ReturnType<typeof setTimeout> | undefined;
    let fallbackActive = false;
    let opened = false;

    const pollFallback = async () => {
      if (cancelled || !fallbackActive) return;
      try {
        await resync();
        if (!cancelled && fallbackActive) setTransport("POLLING FALLBACK");
      } catch {
        if (!cancelled && fallbackActive) setTransport("OFFLINE");
      }
      if (!cancelled && fallbackActive) pollTimer = setTimeout(pollFallback, pollIntervalMs);
    };
    const startFallback = () => {
      if (fallbackActive || cancelled) return;
      fallbackActive = true;
      setTransport("POLLING FALLBACK");
      void pollFallback();
    };
    const scheduleFallback = () => {
      if (fallbackTimer === undefined) fallbackTimer = setTimeout(startFallback, fallbackDelayMs);
    };
    const safeResync = async () => {
      try { await resync(); } catch { if (!cancelled) setTransport("OFFLINE"); }
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
          capabilities: update.payload.capabilities });
      } catch { void safeResync(); }
    };
    const start = async () => {
      try { await resync(); } catch { setTransport("OFFLINE"); }
      finally { if (!cancelled) setLoading(false); }
      if (cancelled) return;
      if (!eventSourceFactory) { startFallback(); return; }
      source = eventSourceFactory(STREAM_ENDPOINT);
      source.addEventListener("live_state", applyLive);
      source.addEventListener("settings", applySettings);
      source.onopen = () => {
        const recovering = opened;
        opened = true;
        fallbackActive = false;
        if (pollTimer !== undefined) clearTimeout(pollTimer);
        if (fallbackTimer !== undefined) clearTimeout(fallbackTimer);
        fallbackTimer = undefined;
        setTransport("REALTIME");
        if (recovering) void safeResync();
      };
      source.onerror = () => { setTransport("RECONNECTING"); scheduleFallback(); };
    };
    void start();
    return () => {
      cancelled = true;
      source?.close();
      if (fallbackTimer !== undefined) clearTimeout(fallbackTimer);
      if (pollTimer !== undefined) clearTimeout(pollTimer);
      if (staleTimer !== undefined) clearTimeout(staleTimer);
    };
  }, [eventSourceFactory, fallbackDelayMs, pollIntervalMs, resync, install]);

  return { snapshot, lastSuccessfulRefresh, offline: transport === "OFFLINE", loading, transport };
}
