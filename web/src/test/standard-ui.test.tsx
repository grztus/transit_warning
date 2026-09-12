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

  it.each([320, 360, 390])("shows only a summary row and preserves the expanded controls and draft at %s px", async width => {
    const previous = window.innerWidth;
    window.innerWidth = width;
    const mutate = vi.fn();
    try {
      render(<App client={async () => fixed} eventSourceFactory={quietStream} settingsMutator={mutate} />);
      const expand = await screen.findByRole("button", { name: "Expand Controls panel" });
      const panel = expand.closest(".controls-panel")! as HTMLElement;
      expect(panel).toHaveClass("controls-compact");
      expect(within(panel).getAllByRole("button")).toEqual([expand]);
      expect(expand).toHaveTextContent("ControlsObserver STATIC");
      expect(expand.querySelector("strong, b")).toBeNull();
      expect(expand.querySelector(".disclosure-title")).toHaveTextContent("Controls");
      expect(expand.querySelector(".observer-summary-state")).not.toHaveClass("summary-warning");
      expect(expand).toHaveAttribute("aria-expanded", "false");
      expect(panel.querySelector("#controls-content")).not.toBeVisible();
      expect(within(panel).queryByRole("alert")).not.toBeInTheDocument();
      expect(within(panel).queryByRole("status")).not.toBeInTheDocument();
      fireEvent.click(expand);
      expect(panel).toHaveClass("controls-expanded");
      for (const name of ["STATIC", "MOBILE", "Observer fallback", "LOCAL", "INTERNET", "AUTO", "Telegram SUN", "Telegram MOON", "Change location"]) {
        expect(within(panel).getByRole("button", { name })).toBeVisible();
      }
      for (const selector of [".controls-meta", ".source-meta", ".observer-meta", ".static-location"]) {
        expect(panel.querySelector(selector)).toBeVisible();
      }
      const staticButton = screen.getByRole("button", { name: "STATIC" });
      const telegram = screen.getByRole("button", { name: "Telegram SUN" });
      const pressed = telegram.getAttribute("aria-pressed");
      fireEvent.click(screen.getByRole("button", { name: "Change location" }));
      fireEvent.change(screen.getByLabelText("Static latitude"), { target: { value: "12" } });
      fireEvent.click(screen.getByRole("button", { name: "Collapse Controls panel" }));
      expect(within(panel).getAllByRole("button")).toEqual([expand]);
      expect(screen.queryByRole("textbox", { name: "Static latitude" })).not.toBeInTheDocument();
      fireEvent.click(expand);
      expect(screen.getByLabelText("Static latitude")).toHaveValue("12");
      expect(screen.getByRole("button", { name: "STATIC" })).toBe(staticButton);
      expect(staticButton).toHaveAttribute("aria-pressed", "true");
      expect(telegram).toHaveAttribute("aria-pressed", pressed);
      expect(mutate).not.toHaveBeenCalled();
    } finally { window.innerWidth = previous; }
  });

  it.each([
    ["MOBILE_FRESH", false, "MOBILE"],
    ["MOBILE_NO_FIX", false, "MOBILE NO FIX"],
    ["MOBILE_LAST_KNOWN", false, "MOBILE STALE"],
    ["STATIC", true, "MOBILE FALLBACK"],
  ])("keeps authoritative %s concise while collapsed", async (effective, fallback, summary) => {
    const current = { ...activeFixture, observer: { ...activeFixture.observer,
      effective_source: effective, fallback_active: fallback } };
    render(<App client={async () => current} eventSourceFactory={quietStream} />);
    const expand = await screen.findByRole("button", { name: "Expand Controls panel" });
    const panel = expand.closest(".controls-panel")! as HTMLElement;
    expect(expand.querySelector("#controls-observer-summary")).toHaveTextContent(`Observer ${summary}`);
    expect(expand.querySelector("#controls-observer-summary")).toBeVisible();
    expect(expand.querySelector(".observer-summary-state")?.classList.contains("summary-warning")).toBe(summary !== "MOBILE");
    expect(within(panel).getAllByRole("button")).toEqual([expand]);
    expect(within(panel).queryByRole("status")).not.toBeInTheDocument();
    fireEvent.click(expand);
    expect(screen.getByRole("button", { name: "Start GPS" })).toBeVisible();
    expect(screen.getByRole("button", { name: "MOBILE" })).toHaveAttribute("aria-pressed", "true");
  });

  it.each([
    [false, true, "Browser GPS requires a secure context (HTTPS)"],
    [true, false, "Browser GPS is unavailable; use HTTPS or a supported browser"],
  ])("distinguishes secure context %s and geolocation availability %s", async (secure, available, message) => {
    const browser = browserGps(); const gps = gpsMock();
    const descriptor = Object.getOwnPropertyDescriptor(window, "isSecureContext");
    Object.defineProperty(window, "isSecureContext", { configurable: true, value: secure });
    if (!available) Object.defineProperty(navigator, "geolocation", { configurable: true, value: undefined });
    const rendered = render(<App client={async () => activeFixture} eventSourceFactory={quietStream} gpsClient={gps} />);
    fireEvent.click(await screen.findByRole("button", { name: "Expand Controls panel" }));
    try {
      const start = await screen.findByRole("button", { name: "Start GPS" });
      await waitFor(() => expect(start).not.toBeDisabled());
      fireEvent.click(start);
      expect(screen.getByRole("alert")).toHaveTextContent(message);
      expect(browser.watchPosition).not.toHaveBeenCalled();
      expect(gps.update).not.toHaveBeenCalled();
    } finally {
      rendered.unmount(); browser.restore();
      if (descriptor) Object.defineProperty(window, "isSecureContext", descriptor);
      else Reflect.deleteProperty(window, "isSecureContext");
    }
  });

  it.each([[1, "permission was denied"], [2, "position is unavailable"], [3, "request timed out"]])(
    "keeps backend no-fix authoritative through browser error %s", async (code, message) => {
      const browser = browserGps(); const gps = gpsMock();
      const noFix = { ...activeFixture, observer: { ...activeFixture.observer, effective_source: "MOBILE_NO_FIX" } };
      const rendered = render(<App client={async () => noFix} eventSourceFactory={quietStream} gpsClient={gps} />);
    fireEvent.click(await screen.findByRole("button", { name: "Expand Controls panel" }));
      try {
        const start = await screen.findByRole("button", { name: "Start GPS" });
        await waitFor(() => expect(start).not.toBeDisabled());
        fireEvent.click(start);
        expect(screen.getByRole("button", { name: "Stop GPS" })).toHaveClass("gps-waiting");
        await act(async () => browser.success[0](fix));
        expect(screen.getByRole("button", { name: "Stop GPS" })).toHaveClass("gps-waiting");
        expect(document.querySelector("#controls-observer-summary")).toHaveTextContent("Observer MOBILE NO FIX");
        act(() => browser.failure[0]({ code } as GeolocationPositionError));
        expect(screen.getByRole("alert")).toHaveTextContent(message);
        expect(screen.getByRole("button", { name: "Stop GPS" })).toHaveClass("gps-attention");
      } finally { rendered.unmount(); browser.restore(); }
    });

  it.each(["LOCAL", "INTERNET", "AUTO"] as const)("sends %s through the settings API without inferring source authority", async mode => {
    const mutate = vi.fn().mockResolvedValue({});
    render(<App client={async () => fixed} eventSourceFactory={quietStream} settingsMutator={mutate} />);
    fireEvent.click(await screen.findByRole("button", { name: "Expand Controls panel" }));
    fireEvent.click(await screen.findByRole("button", { name: mode }));
    await waitFor(() => expect(mutate).toHaveBeenCalledWith(expect.objectContaining({
      expected_revision: 3, changes: { aircraft_source: { requested_mode: mode } },
    })));
    expect(screen.getByRole("button", { name: "LOCAL" })).toHaveAttribute("aria-pressed", "true");
    expect(document.querySelector(".source-meta")).toHaveTextContent("Requested AUTO / Effective LOCAL / Provider ADSB.lol / Health PRIVACY_BLOCKED");
  });

  it.each([320, 360, 390])("keeps Backend state to one disclosure row at %s px", async width => {
    const previous = window.innerWidth;
    window.innerWidth = width;
    const client = vi.fn(async () => fixed);
    try {
      render(<App client={client} eventSourceFactory={quietStream} />);
      const toggle = await screen.findByRole("button", { name: "Expand Backend state panel" });
      const panel = toggle.closest(".status-strip")! as HTMLElement;
      const summary = toggle.textContent;
      expect(panel).toHaveClass("backend-compact");
      expect(panel.children).toHaveLength(1);
      expect(within(panel).getAllByRole("button")).toEqual([toggle]);
      expect(toggle).toHaveAttribute("aria-expanded", "false");
      expect(toggle.children).toHaveLength(3);
      expect(toggle.querySelector("strong, b")).toBeNull();
      expect(toggle.querySelector(".disclosure-title")).toHaveTextContent("Backend state");
      expect(toggle.querySelector(".backend-summary-health")).toHaveClass("summary-active");
      expect(toggle).toHaveTextContent("Backend state");
      expect(toggle).toHaveTextContent(/ACTIVE.*\d{2}:\d{2}:\d{2} UTC/);
      expect(panel.querySelector(".status-details")).toBeNull();
      fireEvent.click(toggle);
      expect(panel).toHaveClass("backend-expanded");
      for (const name of ["Transport", "Last refresh", "Aircraft source requested", "Effective", "Provider", "Provider health", "Generated timestamp"]) {
        expect(within(panel).getByText(name)).toBeVisible();
      }
      expect(within(panel).getByText(fixed.generated_at_utc!)).toBeVisible();
      fireEvent.click(toggle);
      expect(panel.children).toHaveLength(1);
      expect(toggle.textContent).toBe(summary);
      expect(client).toHaveBeenCalledTimes(1);
    } finally { window.innerWidth = previous; }
  });

  // ERROR is a defensive wire-display case; the current typed contract is ACTIVE/STALE.
  it.each(["ACTIVE", "STALE", "ERROR"] as const)("keeps %s visible in the collapsed backend summary", async health => {
    render(<App client={async () => ({ ...fixed, health: health as BootstrapDto["health"] })} eventSourceFactory={quietStream} />);
    const toggle = await screen.findByRole("button", { name: "Expand Backend state panel" });
    expect(toggle).toHaveTextContent(health);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  it("keeps backend health, transport, generated time and last refresh distinct", async () => {
    render(<App client={async () => ({ ...fixed, health: "STALE" })} eventSourceFactory={quietStream} />);
    fireEvent.click(await screen.findByRole("button", { name: "Expand Controls panel" }));
    const button = await screen.findByRole("button", { name: "Expand Backend state panel" });
    expect(button).toHaveTextContent("STALE");
    expect(button).toHaveTextContent(/\d{2}:\d{2}:\d{2} UTC/);
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
    fireEvent.click(await screen.findByRole("button", { name: "Expand Controls panel" }));
    try {
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
    fireEvent.click(await screen.findByRole("button", { name: "Expand Controls panel" }));
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
    fireEvent.click(await screen.findByRole("button", { name: "Expand Controls panel" }));
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
    fireEvent.click(await screen.findByRole("button", { name: "Expand Controls panel" }));
    try {
      await waitFor(() => expect(gps.status).toHaveBeenCalled());
      expect(document.querySelector("#controls-observer-summary")).toHaveTextContent("Observer STATIC");
      fireEvent.click(await screen.findByRole("button", { name: "MOBILE" }));
      await waitFor(() => expect(browser.watchPosition).toHaveBeenCalledTimes(1));
      expect(screen.getByRole("button", { name: "Stop GPS" })).toHaveClass("gps-waiting");
      expect(screen.getByText("Waiting for browser GPS position")).toBeVisible();
      fireEvent.click(screen.getByRole("button", { name: "MOBILE" }));
      expect(browser.watchPosition).toHaveBeenCalledTimes(1);
      current = { ...current, observer: { requested_mode: "MOBILE", effective_source: "MOBILE_FRESH" } };
      await act(async () => browser.success[0](fix));
      await waitFor(() => expect(screen.getByRole("button", { name: "Stop GPS" })).toHaveClass("gps-active"));
      fireEvent.click(screen.getByRole("button", { name: "Collapse Controls panel" }));
      expect(document.querySelector("#controls-observer-summary")).toHaveTextContent("Observer MOBILE");
      expect(screen.queryByRole("button", { name: "Stop GPS" })).not.toBeInTheDocument();
      expect(browser.clearWatch).not.toHaveBeenCalled();
      expect(gps.clear).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: "Expand Controls panel" }));
      expect(screen.getByRole("button", { name: "Stop GPS" })).toHaveClass("gps-active");
      expect(browser.watchPosition).toHaveBeenCalledTimes(1);
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
