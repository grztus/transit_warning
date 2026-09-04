import { afterEach, describe, expect, it, vi } from "vitest";
import { BOOTSTRAP_ENDPOINT, fetchBootstrap } from "../api";
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
});
