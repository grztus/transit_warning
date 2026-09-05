import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import App from "../App";
import { SettingsConflictError } from "../api";
import type { SettingsSnapshotDto } from "../types";
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
    expect(await screen.findByRole("alert")).toHaveTextContent("failed");
    expect(sun).not.toBeDisabled();
    expect(sun).toHaveAttribute("aria-pressed", "true");
  });

  it("shows requested MOBILE with authoritative STATIC fallback degradation", async () => {
    const degraded = { ...activeFixture, observer: { ...activeFixture.observer,
      effective_source: "STATIC", fallback_active: true, gps_health: "UNAVAILABLE" } };
    render(<App client={async () => degraded} eventSourceFactory={quietStream} />);
    expect(await screen.findByText(/MOBILE requested.*effective STATIC/)).toBeInTheDocument();
  });
});
