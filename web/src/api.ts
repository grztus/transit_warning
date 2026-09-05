import type { BootstrapDto, HistoryPageDto, HistoryQuery, SettingsMutation, SettingsSnapshotDto } from "./types";

export const BOOTSTRAP_ENDPOINT = "/api/v1/bootstrap";
export const STREAM_ENDPOINT = "/api/v1/stream";
export const POLL_INTERVAL_MS = 3_000;
export const MAX_RETRY_INTERVAL_MS = 30_000;

export type BootstrapFetcher = () => Promise<BootstrapDto>;
export type SettingsMutator = (mutation: SettingsMutation) => Promise<SettingsSnapshotDto>;
export type HistoryFetcher = (query: HistoryQuery) => Promise<HistoryPageDto>;

export class SettingsConflictError extends Error {
  constructor() { super("Settings revision conflict"); }
}

export function normalizeHistoryMaxSep(value: string | undefined): string | undefined {
  const text = value?.trim();
  if (!text) return undefined;
  const canonical = text.replace(",", ".");
  if (!/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(canonical)) {
    throw new Error("Invalid maximum separation");
  }
  const number = Number(canonical);
  if (!Number.isFinite(number) || number < 0) throw new Error("Invalid maximum separation");
  return canonical;
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

export async function fetchHistory(query: HistoryQuery): Promise<HistoryPageDto> {
  const parameters = new URLSearchParams({
    offset: String(query.offset ?? 0), limit: String(query.limit ?? 25), body: query.body ?? "ALL",
  });
  if (query.date) parameters.set("date", query.date);
  if (query.callsign?.trim()) parameters.set("callsign", query.callsign.trim());
  const maxSepDeg = normalizeHistoryMaxSep(query.maxSepDeg);
  if (maxSepDeg) parameters.set("max_sep_deg", maxSepDeg);
  const response = await fetch(`/api/history?${parameters}`, {
    headers: { Accept: "application/json" }, cache: "no-store",
  });
  if (!response.ok) throw new Error(`History request failed (${response.status})`);
  return await response.json() as HistoryPageDto;
}
