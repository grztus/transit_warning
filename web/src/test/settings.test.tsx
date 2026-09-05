import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import App from "../App";
import { SettingsConflictError } from "../api";
import type { MobileGpsPositionDto, SettingsSnapshotDto } from "../types";
import type { EventSourceFactory } from "../useBootstrap";
import { activeFixture } from "./fixture";

const accepted = (revision: number): SettingsSnapshotDto => ({
  schema_version: 1, revision, values: activeFixture.settings,
  capabilities: activeFixture.capabilities,
  persistence: "RUNTIME_ONLY_RESET_TO_CONFIG_ON_RESTART",
});
const quietStream: EventSourceFactory = () => ({ close() {}, addEventListener() {},
  onopen: null, onerror: null });

describe("synchronized operational controls", () => {
  it("starts browser GPS, submits the existing payload, and stops cleanly", async () => {
    let success!: PositionCallback;
    const watchPosition = vi.fn((next: PositionCallback) => {
      success = next;
      return 17;
    });
    const clearWatch = vi.fn();
    const original = navigator.geolocation;
    Object.defineProperty(navigator, "geolocation", { configurable: true,
      value: { watchPosition, clearWatch } });
    const update = vi.fn().mockResolvedValue({ available: true, status: "ACTIVE" });
    const clear = vi.fn().mockResolvedValue({ available: true, status: "OFF" });
    const gpsClient = { status: vi.fn().mockResolvedValue({ available: true, status: "OFF" }),
      update, clear };
    const rendered = render(<App client={async () => activeFixture} gpsClient={gpsClient}
      eventSourceFactory={quietStream} />);
    try {
      fireEvent.click(await screen.findByRole("button", { name: "Start GPS" }));
      expect(watchPosition).toHaveBeenCalledWith(expect.any(Function), expect.any(Function),
        { enableHighAccuracy: true, timeout: 10_000, maximumAge: 0 });
      const payload: MobileGpsPositionDto = { latitude: 51.123456, longitude: 21.654321,
        accuracy: 7.5, altitude: 240, altitudeAccuracy: 12, timestamp: 123456789 };
      success({ coords: { ...payload, speed: null, heading: null } as unknown as GeolocationCoordinates,
        timestamp: payload.timestamp } as GeolocationPosition);
      await waitFor(() => expect(update).toHaveBeenCalledWith(payload));
      expect(document.body).not.toHaveTextContent("51.123456");
      expect(document.body).not.toHaveTextContent("21.654321");
      fireEvent.click(screen.getByRole("button", { name: "Stop GPS" }));
      await waitFor(() => expect(clear).toHaveBeenCalledTimes(1));
      expect(clearWatch).toHaveBeenCalledWith(17);
      expect(await screen.findByRole("button", { name: "Start GPS" })).toBeInTheDocument();
    } finally {
      rendered.unmount();
      Object.defineProperty(navigator, "geolocation", {
        configurable: true, value: original,
      });
    }
  });

  it("surfaces browser GPS permission errors and clears the watch on cleanup", async () => {
    let failure!: PositionErrorCallback;
    const watchPosition = vi.fn((_next: PositionCallback, error: PositionErrorCallback) => {
      failure = error;
      return 23;
    });
    const clearWatch = vi.fn();
    const original = navigator.geolocation;
    Object.defineProperty(navigator, "geolocation", { configurable: true,
      value: { watchPosition, clearWatch } });
    const gpsClient = { status: vi.fn().mockResolvedValue({ available: true, status: "OFF" }),
      update: vi.fn(), clear: vi.fn() };
    const rendered = render(<App client={async () => activeFixture} gpsClient={gpsClient}
      eventSourceFactory={quietStream} />);
    try {
      fireEvent.click(await screen.findByRole("button", { name: "Start GPS" }));
      failure({ code: 1 } as GeolocationPositionError);
      expect(await screen.findByRole("alert")).toHaveTextContent("permission was denied");
      rendered.unmount();
      expect(clearWatch).toHaveBeenCalledWith(23);
    } finally {
      Object.defineProperty(navigator, "geolocation", {
        configurable: true, value: original,
      });
    }
  });

  it("sends STATIC to MOBILE and MOBILE to STATIC mode requests", async () => {
    const staticFixture = { ...activeFixture, settings: { ...activeFixture.settings,
      observer: { ...activeFixture.settings.observer, requested_mode: "STATIC" as const } } };
    const mobileMutate = vi.fn().mockResolvedValue(accepted(4));
    render(<App client={async () => staticFixture} settingsMutator={mobileMutate}
      eventSourceFactory={quietStream} />);
    fireEvent.click(await screen.findByRole("button", { name: "MOBILE" }));
    await waitFor(() => expect(mobileMutate).toHaveBeenCalledWith(expect.objectContaining({
      expected_revision: 3, changes: { observer: { requested_mode: "MOBILE" } },
    })));

    const staticButton = screen.getByRole("button", { name: "STATIC" });
    await waitFor(() => expect(staticButton).not.toBeDisabled());
    fireEvent.click(staticButton);
    await waitFor(() => expect(mobileMutate).toHaveBeenLastCalledWith(expect.objectContaining({
      changes: { observer: { requested_mode: "STATIC" } },
    })));
  });

  it("opens a blank editor without a request for first-ever MANUAL selection", async () => {
    const staticFixture = { ...activeFixture, settings: { ...activeFixture.settings,
      observer: { ...activeFixture.settings.observer, requested_mode: "STATIC" as const,
        manual_position_saved: false } } };
    const mutate = vi.fn();
    render(<App client={async () => staticFixture} settingsMutator={mutate}
      eventSourceFactory={quietStream} />);
    fireEvent.click(await screen.findByRole("button", { name: "MANUAL" }));
    expect(await screen.findByLabelText("Manual latitude")).toHaveValue("");
    expect(screen.getByLabelText("Manual longitude")).toHaveValue("");
    expect(screen.getByLabelText("Manual elevation AMSL")).toHaveValue("");
    expect(mutate).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "MANUAL" }))
      .toHaveAttribute("aria-pressed", "false");
  });

  it("saves a complete first MANUAL tuple and activates it atomically", async () => {
    const staticFixture = { ...activeFixture, settings: { ...activeFixture.settings,
      observer: { ...activeFixture.settings.observer, requested_mode: "STATIC" as const,
        manual_position_saved: false } } };
    const mutate = vi.fn().mockResolvedValue(accepted(4));
    render(<App client={async () => staticFixture} settingsMutator={mutate}
      eventSourceFactory={quietStream} />);
    fireEvent.click(await screen.findByRole("button", { name: "MANUAL" }));
    fireEvent.change(screen.getByLabelText("Manual latitude"), { target: { value: "51,5" } });
    fireEvent.change(screen.getByLabelText("Manual longitude"), { target: { value: "22.25" } });
    fireEvent.change(screen.getByLabelText("Manual elevation AMSL"), { target: { value: "320" } });
    fireEvent.click(screen.getByRole("button", { name: "SAVE" }));
    await waitFor(() => expect(mutate).toHaveBeenCalledWith(expect.objectContaining({
      changes: { observer: { requested_mode: "MANUAL", manual_lat_deg: 51.5,
        manual_lon_deg: 22.25, manual_elevation_amsl_m: 320 } },
    })));
  });

  it("activates an existing saved MANUAL tuple directly", async () => {
    const staticFixture = { ...activeFixture, settings: { ...activeFixture.settings,
      observer: { ...activeFixture.settings.observer, requested_mode: "STATIC" as const,
        manual_position_saved: true } } };
    const mutate = vi.fn().mockResolvedValue(accepted(4));
    render(<App client={async () => staticFixture} settingsMutator={mutate}
      eventSourceFactory={quietStream} />);
    fireEvent.click(await screen.findByRole("button", { name: "MANUAL" }));
    await waitFor(() => expect(mutate).toHaveBeenCalledWith(expect.objectContaining({
      changes: { observer: { requested_mode: "MANUAL" } },
    })));
  });

  it("edits validated MANUAL coordinates through authoritative settings", async () => {
    const manualFixture = { ...activeFixture, observer: {
      requested_mode: "MANUAL", effective_source: "MANUAL",
      effective_elevation_m: 315, manual_lat_deg: 51.25,
      manual_lon_deg: 21.5, manual_elevation_amsl_m: 315,
    }, settings: { ...activeFixture.settings, observer: {
      ...activeFixture.settings.observer, requested_mode: "MANUAL" as const,
      manual_lat_deg: 51.25, manual_lon_deg: 21.5,
      manual_elevation_amsl_m: 315, manual_position_saved: true,
    } } };
    const mutate = vi.fn().mockResolvedValue(accepted(4));
    render(<App client={async () => manualFixture} settingsMutator={mutate}
      eventSourceFactory={quietStream} />);
    expect(await screen.findByLabelText("Manual latitude")).toHaveValue("51.25");
    fireEvent.change(screen.getByLabelText("Manual latitude"), {
      target: { value: "51,5" },
    });
    fireEvent.change(screen.getByLabelText("Manual longitude"), {
      target: { value: "22.25" },
    });
    fireEvent.change(screen.getByLabelText("Manual elevation AMSL"), {
      target: { value: "320" },
    });
    fireEvent.click(screen.getByRole("button", { name: "SAVE" }));
    await waitFor(() => expect(mutate).toHaveBeenCalledWith(expect.objectContaining({
      changes: { observer: { requested_mode: "MANUAL", manual_lat_deg: 51.5, manual_lon_deg: 22.25,
        manual_elevation_amsl_m: 320 } },
    })));
  });

  it("does not submit invalid MANUAL coordinates", async () => {
    const manualFixture = { ...activeFixture, settings: { ...activeFixture.settings,
      observer: { ...activeFixture.settings.observer, requested_mode: "MANUAL" as const,
        manual_position_saved: true } } };
    const mutate = vi.fn();
    render(<App client={async () => manualFixture} settingsMutator={mutate}
      eventSourceFactory={quietStream} />);
    fireEvent.change(await screen.findByLabelText("Manual latitude"), {
      target: { value: "91" },
    });
    fireEvent.click(screen.getByRole("button", { name: "SAVE" }));
    expect(screen.getByRole("alert")).toHaveTextContent("valid LAT");
    expect(mutate).not.toHaveBeenCalled();
  });

  it("renders authoritative values and sends current revision with unique command ids", async () => {
    const next = { ...activeFixture, settings_revision: 4, settings: {
      ...activeFixture.settings, telegram: { ...activeFixture.settings.telegram, sun_enabled: false },
    } };
    const client = vi.fn().mockResolvedValueOnce(activeFixture).mockResolvedValue(next);
    const mutate = vi.fn().mockResolvedValue(accepted(4));
    render(<App client={client} settingsMutator={mutate} eventSourceFactory={quietStream} />);
    const sun = await screen.findByRole("button", { name: "Telegram SUN" });
    expect(sun).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(sun);
    expect(screen.getByRole("button", { name: "Telegram SUN" })).toHaveTextContent("CHANGING");
    await waitFor(() => expect(mutate).toHaveBeenCalledTimes(1));
    expect(mutate.mock.calls[0][0]).toMatchObject({ expected_revision: 3,
      changes: { telegram: { sun_enabled: false } } });
    const firstId = mutate.mock.calls[0][0].command_id;
    await waitFor(() => expect(screen.getByRole("button", { name: "Telegram SUN" }))
      .toHaveAttribute("aria-pressed", "false"));

    fireEvent.click(screen.getByRole("button", { name: "Telegram MOON" }));
    await waitFor(() => expect(mutate).toHaveBeenCalledTimes(2));
    expect(mutate.mock.calls[1][0].command_id).not.toBe(firstId);
    expect(mutate.mock.calls[1][0].expected_revision).toBe(4);
  });

  it.each([
    ["Telegram MOON", { telegram: { moon_enabled: false } }],
    ["STATIC", { observer: { requested_mode: "STATIC" } }],
    ["Observer fallback", { observer: { fallback_enabled: false } }],
  ])("shows pending and submits %s", async (name, changes) => {
    let finish!: (value: SettingsSnapshotDto) => void;
    const mutate = vi.fn(() => new Promise<SettingsSnapshotDto>((resolve) => { finish = resolve; }));
    render(<App client={async () => activeFixture} settingsMutator={mutate}
      eventSourceFactory={quietStream} />);
    const button = await screen.findByRole("button", { name });
    fireEvent.click(button);
    expect(button).toBeDisabled();
    expect((mutate.mock.calls as unknown as Array<[{ changes: unknown }]>)[0][0].changes)
      .toEqual(changes);
    finish(accepted(4));
    await waitFor(() => expect(button).not.toBeDisabled());
  });

  it("resyncs on conflict without retrying and preserves authoritative state", async () => {
    const current = { ...activeFixture, settings_revision: 4, settings: {
      ...activeFixture.settings, telegram: { ...activeFixture.settings.telegram, sun_enabled: false },
    } };
    const client = vi.fn().mockResolvedValueOnce(activeFixture).mockResolvedValue(current);
    const mutate = vi.fn().mockRejectedValue(new SettingsConflictError());
    render(<App client={client} settingsMutator={mutate} eventSourceFactory={quietStream} />);
    fireEvent.click(await screen.findByRole("button", { name: "Telegram SUN" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("another client");
    expect(mutate).toHaveBeenCalledTimes(1);
    expect(client).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "Telegram SUN" }))
      .toHaveAttribute("aria-pressed", "false");
  });

  it("keeps authoritative state and clears pending after failure", async () => {
    const mutate = vi.fn().mockRejectedValue(new Error("network"));
    render(<App client={async () => activeFixture} settingsMutator={mutate}
      eventSourceFactory={quietStream} />);
    const sun = await screen.findByRole("button", { name: "Telegram SUN" });
    fireEvent.click(sun);
    expect(await screen.findByRole("alert")).toHaveTextContent("network");
    expect(sun).not.toBeDisabled();
    expect(sun).toHaveAttribute("aria-pressed", "true");
  });

  it("shows the useful API validation message", async () => {
    const staticFixture = { ...activeFixture, settings: { ...activeFixture.settings,
      observer: { ...activeFixture.settings.observer, requested_mode: "STATIC" as const } } };
    const mutate = vi.fn().mockRejectedValue(
      new Error("Mobile GPS is disabled"));
    render(<App client={async () => staticFixture} settingsMutator={mutate}
      eventSourceFactory={quietStream} />);
    fireEvent.click(await screen.findByRole("button", { name: "MOBILE" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Mobile GPS is disabled");
  });

  it("shows requested MOBILE with authoritative STATIC fallback degradation", async () => {
    const degraded = { ...activeFixture, observer: { ...activeFixture.observer,
      effective_source: "STATIC", fallback_active: true, gps_health: "UNAVAILABLE" } };
    render(<App client={async () => degraded} eventSourceFactory={quietStream} />);
    expect(await screen.findByText(/MOBILE requested.*effective STATIC/)).toBeInTheDocument();
  });
});
