import type { BootstrapDto, SettingsMutation, SettingsSnapshotDto } from "./types";

export const BOOTSTRAP_ENDPOINT = "/api/v1/bootstrap";
export const STREAM_ENDPOINT = "/api/v1/stream";
export const POLL_INTERVAL_MS = 3_000;
export const MAX_RETRY_INTERVAL_MS = 30_000;

export type BootstrapFetcher = () => Promise<BootstrapDto>;
export type SettingsMutator = (mutation: SettingsMutation) => Promise<SettingsSnapshotDto>;

export class SettingsConflictError extends Error {
  constructor() { super("Settings revision conflict"); }
}

export async function fetchBootstrap(): Promise<BootstrapDto> {
  const response = await fetch(BOOTSTRAP_ENDPOINT, {
    headers: { Accept: "application/json" },
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(`Bootstrap request failed (${response.status})`);
  }
  const payload: unknown = await response.json();
  if (
    typeof payload !== "object" ||
    payload === null ||
    (payload as { schema_version?: unknown }).schema_version !== 1
  ) {
    throw new Error("Unsupported bootstrap schema");
  }
  return payload as BootstrapDto;
}

export async function patchSettings(mutation: SettingsMutation): Promise<SettingsSnapshotDto> {
  const response = await fetch("/api/v1/settings", {
    method: "PATCH",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(mutation),
  });
  if (response.status === 409) throw new SettingsConflictError();
  if (!response.ok) throw new Error(`Settings request failed (${response.status})`);
  return await response.json() as SettingsSnapshotDto;
}
