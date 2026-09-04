import { act, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import App from "../App";
import type { EventSourceFactory, EventSourceLike } from "../useBootstrap";
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
        recent_events: activeFixture.recent_events, presentation: activeFixture.presentation },
    }));
    expect(await screen.findByText("SSE123")).toBeInTheDocument();
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
      act(() => vi.advanceTimersByTime(10_000));
      expect(screen.getByRole("status")).toHaveTextContent("STALE");
      expect(screen.getByText("REALTIME")).toBeInTheDocument();
    } finally { vi.useRealTimers(); }
  });

  it("applies settings revisions to the read-only display", async () => {
    const { sources } = setup();
    await screen.findByText("TEST123");
    const source = sources[0];
    act(() => source.emit("settings", { schema_version: 1, event: "settings",
      settings_revision: 4, payload: { schema_version: 1, revision: 4,
        values: {}, persistence: "RUNTIME_ONLY_RESET_TO_CONFIG_ON_RESTART",
        capabilities: { runtime_settings: false } } }));
    expect(screen.getByText("Settings revision: 4")).toBeInTheDocument();
    expect(screen.getByText("Runtime settings API: unavailable")).toBeInTheDocument();
  });
});
