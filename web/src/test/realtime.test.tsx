import { act, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import App from "../App";
import { generatedHealth } from "../useBootstrap";
import type { EventSourceFactory, EventSourceLike } from "../useBootstrap";
import type { BootstrapDto } from "../types";
import { activeFixture } from "./fixture";

class FakeEventSource implements EventSourceLike {
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listeners = new Map<string, (event: MessageEvent<string>) => void>();
  closed = false;
  addEventListener(type: string, listener: (event: MessageEvent<string>) => void) {
    this.listeners.set(type, listener);
  }
  emit(type: string, value: unknown) {
    this.listeners.get(type)?.({ data: JSON.stringify(value) } as MessageEvent<string>);
  }
  malformed(type: string) {
    this.listeners.get(type)?.({ data: "{" } as MessageEvent<string>);
  }
  close() { this.closed = true; }
}

const setup = (client = vi.fn(async () => activeFixture), fallbackDelayMs = 50) => {
  const sources: FakeEventSource[] = [];
  const factory = vi.fn(() => {
    const source = new FakeEventSource();
    sources.push(source);
    return source;
  });
  const rendered = render(<App client={client} pollIntervalMs={20}
    eventSourceFactory={factory as EventSourceFactory}
    fallbackDelayMs={fallbackDelayMs} reconnectDelayMs={5} />);
  return { sources, factory, client, ...rendered };
};

describe("SSE realtime transport", () => {
  it("bootstraps once, enters realtime and applies the next live revision", async () => {
    const { sources, client } = setup();
    await screen.findByText("TEST123");
    const source = sources[0];
    act(() => source.onopen?.());
    await waitFor(() => expect(screen.getByText("REALTIME")).toBeInTheDocument());
    act(() => source.emit("live_state", {
      schema_version: 1, event: "live_state", live_revision: 43,
      payload: { generated_at_utc: activeFixture.generated_at_utc,
        bodies: { ...activeFixture.bodies, sun: { ...activeFixture.bodies.sun,
          candidates: [{ ...activeFixture.bodies.sun.candidates[0], callsign: "SSE123" }] } },
        recent_events: [{ ...activeFixture.recent_events[0], callsign: "SSEHISTORY" }],
        presentation: activeFixture.presentation },
    }));
    expect(await screen.findByText("SSE123")).toBeInTheDocument();
    expect(screen.getByText("SSEHISTORY")).toBeInTheDocument();
    expect(screen.queryByText("RECENT1")).not.toBeInTheDocument();
    expect(client).toHaveBeenCalledTimes(1);
  });

  it("ignores duplicate and older revisions", async () => {
    const { sources } = setup();
    await screen.findByText("TEST123");
    const source = sources[0];
    for (const revision of [42, 41]) act(() => source.emit("live_state", {
      schema_version: 1, event: "live_state", live_revision: revision,
      payload: { ...activeFixture, bodies: { ...activeFixture.bodies, sun: {
        ...activeFixture.bodies.sun, candidates: [{ callsign: "OLD" }] } } },
    }));
    expect(screen.queryByText("OLD")).not.toBeInTheDocument();
  });

  it("resyncs bootstrap on a revision gap or malformed event", async () => {
    const newer = { ...activeFixture, live_revision: 50,
      bodies: { ...activeFixture.bodies, sun: { ...activeFixture.bodies.sun,
        candidates: [{ ...activeFixture.bodies.sun.candidates[0], callsign: "RESYNC" }] } } };
    const client = vi.fn().mockResolvedValueOnce(activeFixture).mockResolvedValue(newer);
    const { sources } = setup(client);
    await screen.findByText("TEST123");
    const source = sources[0];
    act(() => source.emit("live_state", { schema_version: 1, event: "live_state",
      live_revision: 45, payload: {} }));
    expect(await screen.findByText("RESYNC")).toBeInTheDocument();
    act(() => source.malformed("live_state"));
    await waitFor(() => expect(client.mock.calls.length).toBeGreaterThanOrEqual(3));
  });

  it("shows reconnecting, activates polling fallback, then recovers", async () => {
    vi.useFakeTimers();
    try {
      const { sources } = setup(undefined, 10);
      await act(async () => { await Promise.resolve(); });
      const source = sources[0];
      act(() => source.onerror?.());
      expect(screen.getByText("RECONNECTING")).toBeInTheDocument();
      await act(async () => { vi.advanceTimersByTime(10); await Promise.resolve(); });
      expect(screen.getByText("POLLING FALLBACK")).toBeInTheDocument();
      await act(async () => { vi.advanceTimersByTime(5); await Promise.resolve(); });
      act(() => sources[1].onopen?.());
      expect(screen.getByText("REALTIME")).toBeInTheDocument();
    } finally { vi.useRealTimers(); }
  });

  it("recreates the stream across repeated failures, stops polling, and cleans up", async () => {
    vi.useFakeTimers();
    try {
      const { sources, factory, client, unmount } = setup(undefined, 10);
      await act(async () => { await Promise.resolve(); });
      act(() => sources[0].onopen?.());

      act(() => sources[0].onerror?.());
      expect(sources[0].closed).toBe(true);
      await act(async () => { vi.advanceTimersByTime(5); await Promise.resolve(); });
      expect(factory).toHaveBeenCalledTimes(2);
      await act(async () => { vi.advanceTimersByTime(5); await Promise.resolve(); });
      expect(screen.getByText("POLLING FALLBACK")).toBeInTheDocument();

      act(() => sources[1].onopen?.());
      await act(async () => { await Promise.resolve(); });
      expect(screen.getByText("REALTIME")).toBeInTheDocument();
      const callsAfterRecovery = client.mock.calls.length;
      await act(async () => { vi.advanceTimersByTime(100); await Promise.resolve(); });
      expect(client).toHaveBeenCalledTimes(callsAfterRecovery);
      expect(factory).toHaveBeenCalledTimes(2);

      act(() => sources[1].onerror?.());
      await act(async () => { vi.advanceTimersByTime(5); await Promise.resolve(); });
      expect(factory).toHaveBeenCalledTimes(3);
      act(() => sources[2].onopen?.());
      expect(screen.getByText("REALTIME")).toBeInTheDocument();

      unmount();
      expect(sources[2].closed).toBe(true);
      act(() => vi.advanceTimersByTime(1_000));
      expect(factory).toHaveBeenCalledTimes(3);
    } finally { vi.useRealTimers(); }
  });

  it("can show stale backend data while transport remains realtime", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-04T10:00:01Z"));
    try {
      const { sources } = setup();
      await act(async () => { await Promise.resolve(); });
      const source = sources[0];
      act(() => source.onopen?.());
      act(() => source.emit("live_state", { schema_version: 1, event: "live_state",
        live_revision: 43, payload: { generated_at_utc: "2026-09-04T10:00:01Z",
          bodies: activeFixture.bodies, recent_events: activeFixture.recent_events,
          presentation: activeFixture.presentation } }));
      expect(screen.getByRole("status")).toHaveTextContent("ACTIVE");
      expect(screen.getByText("REALTIME")).toBeInTheDocument();
      act(() => vi.advanceTimersByTime(10_001));
      expect(screen.getByRole("status")).toHaveTextContent("STALE");
      expect(screen.getByText("REALTIME")).toBeInTheDocument();
    } finally { vi.useRealTimers(); }
  });

  it("applies authoritative settings revisions to the controls", async () => {
    const { sources } = setup();
    await screen.findByText("TEST123");
    const source = sources[0];
    act(() => source.emit("settings", { schema_version: 1, event: "settings",
      settings_revision: 4, payload: { schema_version: 1, revision: 4,
        values: { telegram: { sun_enabled: false, moon_enabled: true },
          observer: { requested_mode: "STATIC", fallback_enabled: false } },
        persistence: "RUNTIME_ONLY_RESET_TO_CONFIG_ON_RESTART",
        capabilities: { runtime_settings: false } } }));
    expect(screen.getByText(/API unavailable · rev 4/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "STATIC" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("RECENT1")).toBeInTheDocument();
  });
});


describe("HTTP and stream ordering", () => {
  it("does not let delayed HTTP undo newer live and settings events", async () => {
    let finish!: (snapshot: BootstrapDto) => void;
    const client = vi.fn().mockResolvedValueOnce(activeFixture)
      .mockImplementation(() => new Promise<BootstrapDto>(resolve => { finish = resolve; }));
    const { sources } = setup(client);
    await screen.findByText("TEST123");
    act(() => sources[0].malformed("live_state"));
    act(() => sources[0].emit("live_state", { schema_version: 1, event: "live_state", live_revision: 43,
      payload: { ...activeFixture, bodies: { ...activeFixture.bodies,
        sun: { current_position: null, candidates: [{ callsign: "LATEST" }] } } } }));
    act(() => sources[0].emit("settings", { schema_version: 1, event: "settings", settings_revision: 4,
      payload: { values: { ...activeFixture.settings, telegram: { sun_enabled: false, moon_enabled: true } },
        capabilities: activeFixture.capabilities } }));
    await act(async () => finish(activeFixture));
    expect(screen.getByText("LATEST")).toBeInTheDocument();
    expect(screen.queryByText("TEST123")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Telegram SUN" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("button", { name: "Telegram MOON" })).toHaveAttribute("aria-pressed", "true");
  });

  it("retains newer HTTP results when concurrent resyncs finish out of order", async () => {
    const finishes: Array<(snapshot: BootstrapDto) => void> = [];
    const client = vi.fn().mockResolvedValueOnce(activeFixture)
      .mockImplementation(() => new Promise<BootstrapDto>(resolve => { finishes.push(resolve); }));
    const { sources } = setup(client);
    await screen.findByText("TEST123");
    act(() => { sources[0].malformed("live_state"); sources[0].malformed("live_state"); });
    await act(async () => finishes[1]({ ...activeFixture, live_revision: 50,
      bodies: { ...activeFixture.bodies, sun: { current_position: null, candidates: [{ callsign: "NEWHTTP" }] } } }));
    await act(async () => finishes[0](activeFixture));
    expect(screen.getByText("NEWHTTP")).toBeInTheDocument();
  });

  it("does not report an open stream offline when auxiliary HTTP fails", async () => {
    const client = vi.fn().mockResolvedValueOnce(activeFixture).mockRejectedValue(new Error("temporary"));
    const { sources } = setup(client);
    await screen.findByText("TEST123");
    await act(async () => { sources[0].onopen?.(); sources[0].malformed("live_state"); });
    await act(async () => sources[0].malformed("live_state"));
    expect(screen.getByText("REALTIME")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("ACTIVE");
    expect(screen.queryByText(/Connection failed/)).not.toBeInTheDocument();
  });
});


describe("release observer and health synchronization", () => {
  it("updates client B observer diagnostics when client A selects STATIC or a custom fixed location", async () => {
    const { sources, client } = setup();
    await screen.findByText("TEST123");
    for (const [revision, mode] of [[4, "STATIC"], [5, "MANUAL"]] as const) {
      act(() => sources[0].emit("settings", { schema_version: 1, event: "settings", settings_revision: revision,
        payload: { values: { ...activeFixture.settings, observer: { ...activeFixture.settings.observer,
          requested_mode: mode, manual_position_saved: true } }, capabilities: activeFixture.capabilities,
          observer: { requested_mode: mode, effective_source: mode, gps_health: "NO_FIX" } } }));
      expect(screen.getByRole("button", { name: "STATIC" })).toHaveAttribute("aria-pressed", "true");
      expect(document.querySelector(".observer-meta")).not.toHaveTextContent("MOBILE");
      expect(document.querySelector(".observer-meta")).toHaveTextContent(mode === "MANUAL" ? "STATIC (custom)" : "STATIC");
      expect(screen.queryByRole("button", { name: "MANUAL" })).not.toBeInTheDocument();
    }
    // A delayed live publication from before the settings change cannot undo it.
    act(() => sources[0].emit("live_state", { schema_version: 1, event: "live_state", live_revision: 43,
      payload: { generated_at_utc: new Date().toISOString(), bodies: activeFixture.bodies,
        recent_events: activeFixture.recent_events, presentation: activeFixture.presentation,
        observer: activeFixture.observer, observer_settings_revision: 3 } }));
    expect(document.querySelector(".observer-meta")).toHaveTextContent("STATIC (custom)");
    expect(client).toHaveBeenCalledTimes(1); // No polling or bootstrap round trip required.
  });

  it("updates effective observer status from live state without changing settings", async () => {
    const { sources } = setup();
    await screen.findByText("TEST123");
    act(() => sources[0].emit("live_state", { schema_version: 1, event: "live_state", live_revision: 43,
      payload: { generated_at_utc: new Date().toISOString(), bodies: activeFixture.bodies,
        recent_events: activeFixture.recent_events, presentation: activeFixture.presentation,
        observer: { ...activeFixture.observer, effective_source: "MOBILE_LAST_KNOWN", gps_health: "STALE" },
        observer_settings_revision: 3 } }));
    expect(document.querySelector(".observer-meta")).toHaveTextContent("Effective MOBILE_LAST_KNOWN");
    expect(screen.getByRole("button", { name: "MOBILE" })).toHaveAttribute("aria-pressed", "true");
  });

  it.each(["diagnostic", "source"])("does not revive stale data on a %s publication", async kind => {
    const fixture = { ...activeFixture, health: "STALE" as const,
      generated_at_utc: new Date(Date.now() - 20_000).toISOString() };
    const { sources } = setup(vi.fn(async () => fixture));
    await screen.findByText("TEST123");
    act(() => sources[0].onopen?.());
    act(() => sources[0].emit("live_state", { schema_version: 1, event: "live_state", live_revision: 43,
      payload: { generated_at_utc: fixture.generated_at_utc, bodies: fixture.bodies,
        recent_events: fixture.recent_events, presentation: fixture.presentation,
        ...(kind === "source" ? { aircraft_source: { requested_mode: "AUTO", status: "HEALTHY" } } : {}) } }));
    expect(screen.getByRole("status")).toHaveTextContent("STALE");
    expect(screen.getByText("REALTIME")).toBeInTheDocument();
  });

  it("ages fresh data from its generated time instead of its SSE receipt time", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-12T12:00:00Z"));
    try {
      const { sources } = setup();
      await act(async () => { await Promise.resolve(); });
      act(() => sources[0].emit("live_state", { schema_version: 1, event: "live_state", live_revision: 43,
        payload: { generated_at_utc: "2026-09-12T11:59:52Z", bodies: activeFixture.bodies,
          recent_events: [], presentation: activeFixture.presentation } }));
      expect(screen.getByRole("status")).toHaveTextContent("ACTIVE");
      act(() => vi.advanceTimersByTime(2_000));
      expect(screen.getByRole("status")).toHaveTextContent("ACTIVE"); // Backend boundary is inclusive.
      act(() => vi.advanceTimersByTime(1));
      expect(screen.getByRole("status")).toHaveTextContent("STALE");
    } finally { vi.useRealTimers(); }
  });

  it("keeps stale data stale across reconnect", async () => {
    const fixture = { ...activeFixture, health: "STALE" as const,
      generated_at_utc: new Date(Date.now() - 20_000).toISOString() };
    const { sources } = setup(vi.fn(async () => fixture));
    await screen.findByText("TEST123");
    act(() => sources[0].onerror?.());
    await waitFor(() => expect(sources).toHaveLength(2));
    await act(async () => sources[1].onopen?.());
    expect(screen.getByText("REALTIME")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("STALE");
  });

  it("matches the existing ten-second freshness boundary and rejects missing timestamps", () => {
    const now = Date.parse("2026-09-12T12:00:00Z");
    expect(generatedHealth("2026-09-12T11:59:50Z", now)).toBe("ACTIVE");
    expect(generatedHealth("2026-09-12T11:59:49.999Z", now)).toBe("STALE");
    expect(generatedHealth(undefined, now)).toBe("STALE");
    expect(generatedHealth("invalid", now)).toBe("STALE");
  });
});
