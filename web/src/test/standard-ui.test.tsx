import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import App from "../App";
import type { BootstrapDto } from "../types";
import { activeFixture } from "./fixture";

const quietStream = () => ({ close() {}, addEventListener() {}, onopen: null, onerror: null });
const fixed: BootstrapDto = { ...activeFixture, observer: { requested_mode: "STATIC", effective_source: "STATIC" },
  settings: { ...activeFixture.settings, aircraft_source: { requested_mode: "LOCAL" },
    observer: { ...activeFixture.settings.observer, requested_mode: "STATIC", manual_position_saved: false } },
  aircraft_source: { requested_mode: "AUTO", effective_mode: "LOCAL", provider: "ADSB.lol", status: "PRIVACY_BLOCKED" } };
const gpsMock = () => ({ status: vi.fn().mockResolvedValue({ available: true, status: "OFF" }),
  update: vi.fn().mockResolvedValue({ available: true, status: "ACTIVE" }),
  clear: vi.fn().mockResolvedValue({ available: true, status: "OFF" }) });
const fix = { coords: { latitude: 12, longitude: 34, accuracy: 5, altitude: null, altitudeAccuracy: null }, timestamp: 123 } as GeolocationPosition;

function browserGps() {
  const success: PositionCallback[] = [];
  const failure: PositionErrorCallback[] = [];
  const watchPosition = vi.fn((next: PositionCallback, error: PositionErrorCallback) => {
    success.push(next); failure.push(error); return success.length;
  });
  const clearWatch = vi.fn();
  const original = navigator.geolocation;
  Object.defineProperty(navigator, "geolocation", { configurable: true, value: { watchPosition, clearWatch } });
  return { success, failure, watchPosition, clearWatch,
    restore: () => Object.defineProperty(navigator, "geolocation", { configurable: true, value: original }) };
}

describe("STANDARD final controls", () => {
  it("keeps only LIVE/HISTORY navigation and compact accessible controls at narrow widths", async () => {
    const previous = window.innerWidth;
    window.innerWidth = 360;
    try {
      render(<App client={async () => fixed} eventSourceFactory={quietStream}
        historyClient={async () => ({ records: [], offset: 0, limit: 25, next_offset: null, has_more: false })} />);
      const expand = await screen.findByRole("button", { name: "Expand Controls panel" });
      expect(expand).toHaveAttribute("aria-expanded", "false");
      expect(screen.queryByRole("button", { name: "Change location" })).not.toBeInTheDocument();
      expect(within(screen.getByRole("navigation")).getAllByRole("button").map(b => b.textContent)).toEqual(["LIVE", "HISTORY"]);
      expect(screen.queryByRole("button", { name: /MANUAL|PATTERN|Show my location/i })).not.toBeInTheDocument();
      expect(document.querySelector("iframe, canvas")).toBeNull();
      fireEvent.click(expand);
      expect(screen.getByRole("button", { name: "Collapse Controls panel" })).toHaveAttribute("aria-expanded", "true");
      expect(screen.getByText("Default configured location")).toBeVisible();
      expect(screen.getByRole("button", { name: "STATIC" })).toHaveAttribute("aria-pressed", "true");
      fireEvent.click(screen.getByRole("button", { name: "HISTORY" }));
      expect(screen.getByRole("button", { name: "HISTORY" })).toHaveAttribute("aria-pressed", "true");
      expect(screen.getByRole("button", { name: "LIVE" })).toHaveAttribute("aria-pressed", "false");
    } finally { window.innerWidth = previous; }
  });

  it.each(["LOCAL", "INTERNET", "AUTO"] as const)("sends %s through the settings API without inferring source authority", async mode => {
    const mutate = vi.fn().mockResolvedValue({});
    render(<App client={async () => fixed} eventSourceFactory={quietStream} settingsMutator={mutate} />);
    fireEvent.click(await screen.findByRole("button", { name: mode }));
    await waitFor(() => expect(mutate).toHaveBeenCalledWith(expect.objectContaining({
      expected_revision: 3, changes: { aircraft_source: { requested_mode: mode } },
    })));
    expect(screen.getByRole("button", { name: "LOCAL" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Expand Controls panel" }));
    expect(document.querySelector(".source-meta")).toHaveTextContent("Requested AUTO / Effective LOCAL / Provider ADSB.lol / Health PRIVACY_BLOCKED");
  });

  it("keeps backend health, transport, generated time and last refresh distinct", async () => {
    render(<App client={async () => ({ ...fixed, health: "STALE" })} eventSourceFactory={quietStream} />);
    const button = await screen.findByRole("button", { name: "Expand Backend state panel" });
    expect(button).toHaveTextContent("STALE");
    expect(button).toHaveTextContent("Generated");
    expect(screen.getByText("RECONNECTING")).toBeVisible();
    expect(screen.queryByText("Last refresh")).not.toBeInTheDocument();
    fireEvent.click(button);
    expect(screen.getByText("Last refresh")).toBeVisible();
    expect(screen.getByText("Provider health")).toBeVisible();
    expect(screen.getByText("PRIVACY_BLOCKED")).toBeVisible();
  });

  it("applies custom STATIC coordinates without starting or submitting GPS", async () => {
    const browser = browserGps(); const gps = gpsMock();
    let current = fixed;
    const mutate = vi.fn().mockImplementation(async ({ changes }) => {
      current = { ...current, settings_revision: 4, settings: { ...current.settings,
        observer: { ...current.settings.observer, ...changes.observer, manual_position_saved: true } } };
      return {};
    });
    const rendered = render(<App client={async () => current} eventSourceFactory={quietStream} settingsMutator={mutate} gpsClient={gps} />);
    try {
      fireEvent.click(await screen.findByRole("button", { name: "Expand Controls panel" }));
      fireEvent.click(screen.getByRole("button", { name: "Change location" }));
      for (const [name, value] of [["Static latitude", "12"], ["Static longitude", "34"], ["Static elevation AMSL", "100"]]) {
        fireEvent.change(screen.getByLabelText(name), { target: { value } });
      }
      fireEvent.click(screen.getByRole("button", { name: "Apply" }));
      await screen.findByText("Custom fixed location");
      expect(screen.getByRole("button", { name: "STATIC" })).toHaveAttribute("aria-pressed", "true");
      expect(screen.queryByLabelText("Static latitude")).not.toBeInTheDocument();
      expect(browser.watchPosition).not.toHaveBeenCalled(); expect(gps.update).not.toHaveBeenCalled();
    } finally { rendered.unmount(); browser.restore(); }
  });

  it("rejects previous-generation callbacks after Stop GPS and a new watch", async () => {
    const browser = browserGps(); const gps = gpsMock();
    const client = vi.fn(async () => activeFixture);
    const rendered = render(<App client={client} eventSourceFactory={quietStream} gpsClient={gps} />);
    try {
      const start = await screen.findByRole("button", { name: "Start GPS" });
      await waitFor(() => expect(start).not.toBeDisabled());
      fireEvent.click(start);
      fireEvent.click(screen.getByRole("button", { name: "Stop GPS" }));
      await waitFor(() => expect(gps.clear).toHaveBeenCalledTimes(1));
      fireEvent.click(screen.getByRole("button", { name: "Start GPS" }));
      expect(browser.watchPosition).toHaveBeenCalledTimes(2);
      await act(async () => {
        browser.success[0](fix);
        browser.failure[0]({ code: 1 } as GeolocationPositionError);
      });
      expect(gps.update).not.toHaveBeenCalled();
      expect(screen.getByText("Waiting for browser GPS position")).toBeVisible();
      await act(async () => browser.success[1](fix));
      expect(gps.update).toHaveBeenCalledTimes(1);
    } finally { rendered.unmount(); browser.restore(); }
  });

  it("reports synchronous GPS startup failure without submitting coordinates", async () => {
    const browser = browserGps(); const gps = gpsMock();
    browser.watchPosition.mockImplementation(() => { throw new Error("unavailable"); });
    const rendered = render(<App client={async () => activeFixture} eventSourceFactory={quietStream} gpsClient={gps} />);
    try {
      const start = await screen.findByRole("button", { name: "Start GPS" });
      await waitFor(() => expect(start).not.toBeDisabled());
      fireEvent.click(start);
      expect(screen.getByText(/Browser GPS is unavailable/)).toBeVisible();
      expect(screen.getByRole("button", { name: "Start GPS" })).toHaveClass("gps-attention");
      expect(screen.queryByRole("button", { name: "Stop GPS" })).not.toBeInTheDocument();
      expect(gps.update).not.toHaveBeenCalled();
    } finally { rendered.unmount(); browser.restore(); }
  });

  it("starts MOBILE, reuses its watch, shows waiting/active/error and ignores old callbacks after STATIC", async () => {
    const browser = browserGps(); const gps = gpsMock();
    let current = fixed;
    const client = vi.fn(async () => current);
    const mutate = vi.fn().mockImplementation(async ({ changes }) => {
      current = { ...current, settings_revision: current.settings_revision + 1,
        observer: { requested_mode: changes.observer.requested_mode, effective_source: changes.observer.requested_mode },
        settings: { ...current.settings, observer: { ...current.settings.observer, ...changes.observer } } };
      return {};
    });
    const rendered = render(<App client={client} eventSourceFactory={quietStream} settingsMutator={mutate} gpsClient={gps} />);
    try {
      await waitFor(() => expect(gps.status).toHaveBeenCalled());
      fireEvent.click(await screen.findByRole("button", { name: "MOBILE" }));
      await waitFor(() => expect(browser.watchPosition).toHaveBeenCalledTimes(1));
      expect(screen.getByRole("button", { name: "Stop GPS" })).toHaveClass("gps-waiting");
      expect(screen.getByText("Waiting for browser GPS position")).toBeVisible();
      fireEvent.click(screen.getByRole("button", { name: "MOBILE" }));
      expect(browser.watchPosition).toHaveBeenCalledTimes(1);
      current = { ...current, observer: { requested_mode: "MOBILE", effective_source: "MOBILE_FRESH" } };
      await act(async () => browser.success[0](fix));
      await waitFor(() => expect(screen.getByRole("button", { name: "Stop GPS" })).toHaveClass("gps-active"));
      act(() => browser.failure[0]({ code: 1 } as GeolocationPositionError));
      expect(screen.getByRole("button", { name: "Stop GPS" })).toHaveClass("gps-attention");
      expect(screen.getByText(/permission was denied/)).toBeVisible();
      let finish!: (value: unknown) => void;
      gps.update.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
      act(() => browser.success[0](fix));
      fireEvent.click(screen.getByRole("button", { name: "STATIC" }));
      await waitFor(() => expect(screen.getByRole("button", { name: "STATIC" })).not.toBeDisabled());
      expect(browser.clearWatch).toHaveBeenCalledWith(1);
      const calls = client.mock.calls.length;
      await act(async () => { finish({}); browser.success[0](fix); browser.failure[0]({ code: 2 } as GeolocationPositionError); });
      expect(client).toHaveBeenCalledTimes(calls);
      expect(screen.getByRole("button", { name: "STATIC" })).toHaveAttribute("aria-pressed", "true");
      expect(screen.queryByRole("button", { name: "Stop GPS" })).not.toBeInTheDocument();
      expect(gps.update).toHaveBeenCalledTimes(2);
    } finally { rendered.unmount(); browser.restore(); }
  });
});
