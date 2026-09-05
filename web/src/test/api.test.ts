import { afterEach, describe, expect, it, vi } from "vitest";
import { BOOTSTRAP_ENDPOINT, fetchBootstrap, patchSettings, SettingsConflictError } from "../api";
import { activeFixture } from "./fixture";

describe("bootstrap client", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("loads the schema-v1 contract from the versioned endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => activeFixture,
    });
    vi.stubGlobal("fetch", fetchMock);
    await expect(fetchBootstrap()).resolves.toEqual(activeFixture);
    expect(fetchMock).toHaveBeenCalledWith(BOOTSTRAP_ENDPOINT, expect.objectContaining({ cache: "no-store" }));
  });

  it("rejects unsupported schemas", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ schema_version: 2 }) }));
    await expect(fetchBootstrap()).rejects.toThrow("Unsupported bootstrap schema");
  });

  it("patches versioned settings with the supplied CAS command", async () => {
    const result = { schema_version: 1, revision: 4, values: activeFixture.settings,
      capabilities: {}, persistence: "RUNTIME_ONLY_RESET_TO_CONFIG_ON_RESTART" };
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200,
      json: async () => result });
    vi.stubGlobal("fetch", fetchMock);
    const mutation = { expected_revision: 3, command_id: "unique-command",
      changes: { telegram: { sun_enabled: false } } };
    await expect(patchSettings(mutation)).resolves.toEqual(result);
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/settings", expect.objectContaining({
      method: "PATCH", body: JSON.stringify(mutation),
    }));
  });

  it("maps HTTP 409 to an explicit conflict", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 409 }));
    await expect(patchSettings({ expected_revision: 3, command_id: "stale",
      changes: { telegram: { moon_enabled: false } } }))
      .rejects.toBeInstanceOf(SettingsConflictError);
  });
});
