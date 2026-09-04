import type { BootstrapDto } from "./types";

export const BOOTSTRAP_ENDPOINT = "/api/v1/bootstrap";
export const POLL_INTERVAL_MS = 3_000;
export const MAX_RETRY_INTERVAL_MS = 30_000;

export type BootstrapFetcher = () => Promise<BootstrapDto>;

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
