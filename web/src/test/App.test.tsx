import { act, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import App, { formatCountdown } from "../App";
import { activeFixture } from "./fixture";

describe("LIVE screen", () => {
  it.each([undefined, null, "", "TEST123"])(
    "uses callsign or ICAO consistently in LIVE and HISTORY: %j", async (callsign) => {
      const fixture = {
        ...activeFixture,
        bodies: {
          sun: { current_position: null, candidates: [{ icao: "ABC123", callsign }] },
          moon: { current_position: null, candidates: [] },
        },
        recent_events: [{ icao: "ABC123", callsign }],
      };
      const { container } = render(<App client={async () => fixture} pollIntervalMs={60_000} />);
      await screen.findByRole("status");
      await waitFor(() => expect(container.querySelector(".callsign")).toHaveTextContent(
        callsign?.trim() || "ABC123"));
      expect(container.querySelector(".event-row strong")).toHaveTextContent(
        callsign?.trim() || "ABC123");
      if (callsign?.trim()) expect(screen.queryByText("ABC123")).not.toBeInTheDocument();
    });

  it("renders ACTIVE live state, bodies, candidates, history and observer diagnostics", async () => {
    render(<App client={async () => activeFixture} pollIntervalMs={60_000} />);
    expect(await screen.findByRole("status")).toHaveTextContent("ACTIVE");
    expect(screen.getByText("TEST123")).toBeInTheDocument();
    expect(screen.getByText("RECENT1")).toBeInTheDocument();
    expect(screen.getByText(/ALT 31.2° · AZ 217.4°/)).toBeInTheDocument();
    expect(screen.getAllByText("MOBILE")).toHaveLength(3);
    expect(screen.getByText("SEP 0.4°")).toHaveClass("sep-green");
    expect(screen.getByText("CANDIDATE")).toHaveClass("state-candidate");
    expect(screen.getByText("TELEGRAM RANGE")).toHaveClass("state-telegram-range");
    expect(screen.getByText("SEP 1.2°")).toHaveClass("sep-yellow");
    expect(screen.getByText("PASSED")).toHaveClass("state-passed");
    expect(screen.getByText("7:ABC123:SUN:2")).toHaveClass("encounter-id");
  });

  it("keeps backend STALE distinct from fetch failure", async () => {
    render(<App client={async () => ({ ...activeFixture, health: "STALE" })} pollIntervalMs={60_000} />);
    expect(await screen.findByRole("status")).toHaveTextContent("STALE");
    expect(screen.queryByText(/Connection failed/)).not.toBeInTheDocument();
  });

  it("preserves the last valid snapshot and marks it OFFLINE after failure", async () => {
    const client = vi.fn()
      .mockResolvedValueOnce(activeFixture)
      .mockRejectedValue(new Error("offline"));
    render(<App client={client} pollIntervalMs={5} />);
    expect(await screen.findByText("TEST123")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("OFFLINE"));
    expect(screen.getByText("TEST123")).toBeInTheDocument();
    expect(screen.getByText(/showing the last valid snapshot/i)).toBeInTheDocument();
  });

  it("renders only the privacy-safe observer diagnostics from bootstrap", async () => {
    const { container } = render(<App client={async () => activeFixture} pollIntervalMs={60_000} />);
    await screen.findByText("TEST123");
    expect(container).toHaveTextContent("GPS");
    expect(container.innerHTML).not.toMatch(/latitude|longitude|token|filesystem|forensic/i);
  });

  it("handles missing optional fields", async () => {
    const sparse = {
      ...activeFixture,
      observer: {},
      bodies: {
        sun: { current_position: null, candidates: [{}] },
        moon: { current_position: null, candidates: [] },
      },
      recent_events: [{}],
      capabilities: {},
    };
    render(<App client={async () => sparse} pollIntervalMs={60_000} />);
    expect(await screen.findAllByText("NOCALL")).toHaveLength(2);
    expect(screen.getByText("Runtime settings API: unavailable")).toBeInTheDocument();
  });

  it("updates the displayed countdown locally once per second", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-04T10:00:00Z"));
    try {
      render(<App client={async () => activeFixture} pollIntervalMs={60_000} />);
      await act(async () => { await Promise.resolve(); });
      expect(screen.getByText("02:30")).toBeInTheDocument();
      act(() => vi.advanceTimersByTime(1_000));
      expect(screen.getByText("02:29")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("candidate countdown", () => {
  const now = Date.parse("2026-09-04T10:00:00Z");

  it("formats future events below one hour as MM:SS", () => {
    expect(formatCountdown("2026-09-04T10:00:55Z", now)).toBe("00:55");
  });

  it("formats future events at least one hour away as H:MM:SS", () => {
    expect(formatCountdown("2026-09-04T11:01:01Z", now)).toBe("1:01:01");
  });

  it("formats elapsed time with an explicit plus prefix", () => {
    expect(formatCountdown("2026-09-04T09:59:57Z", now)).toBe("+00:03");
  });

  it("returns no countdown for missing or invalid timestamps", () => {
    expect(formatCountdown(undefined, now)).toBeNull();
    expect(formatCountdown("not-a-time", now)).toBeNull();
  });
});
