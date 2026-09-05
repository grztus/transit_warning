import { afterEach, describe, expect, it, vi } from "vitest";
import { BOOTSTRAP_ENDPOINT, fetchBootstrap, fetchHistory, mobileGpsClient, normalizeHistoryMaxSep, patchSettings, SettingsConflictError, SettingsRequestError } from "../api";
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

  it("propagates a useful settings API validation error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 400,
      json: async () => ({ error: "Save a complete MANUAL observer position before activation" }) }));
    await expect(patchSettings({ expected_revision: 3, command_id: "manual",
      changes: { observer: { requested_mode: "MANUAL" } } }))
      .rejects.toEqual(new SettingsRequestError(
        "Save a complete MANUAL observer position before activation"));
  });

  it("posts the legacy-compatible browser GPS payload to the existing endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200,
      json: async () => ({ available: true, status: "ACTIVE" }) });
    vi.stubGlobal("fetch", fetchMock);
    const position = { latitude: 51.1, longitude: 21.2, accuracy: 5,
      altitude: null, altitudeAccuracy: null, timestamp: 1234 };
    await mobileGpsClient.update(position);
    expect(fetchMock).toHaveBeenCalledWith("/api/mobile-gps", expect.objectContaining({
      method: "POST", body: JSON.stringify(position), cache: "no-store",
    }));
  });

  it("queries the existing legacy history endpoint with public filters", async () => {
    const result = { records: [], offset: 0, limit: 25, next_offset: null, has_more: false };
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => result });
    vi.stubGlobal("fetch", fetchMock);
    await expect(fetchHistory({ date: "2026-09-03", callsign: " ABC ", body: "MOON",
      maxSepDeg: "3.0", offset: 25,
      limit: 25 })).resolves.toEqual(result);
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("/api/history?");
    expect(url).toContain("date=2026-09-03");
    expect(url).toContain("callsign=ABC");
    expect(url).toContain("offset=25");
    expect(url).toContain("limit=25");
    expect(url).toContain("body=MOON");
    expect(url).toContain("max_sep_deg=3.0");
  });

  it("normalizes localized history separation decimals", () => {
    expect(normalizeHistoryMaxSep("0.8")).toBe("0.8");
    expect(normalizeHistoryMaxSep("0,8")).toBe("0.8");
    expect(normalizeHistoryMaxSep("")).toBeUndefined();
    expect(() => normalizeHistoryMaxSep("-0.8")).toThrow("Invalid maximum separation");
  });
});
