import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchBootstrap,
  MAX_RETRY_INTERVAL_MS,
  POLL_INTERVAL_MS,
  type BootstrapFetcher,
} from "./api";
import type { BootstrapDto } from "./types";

export interface BootstrapPollingState {
  snapshot: BootstrapDto | null;
  lastSuccessfulRefresh: Date | null;
  offline: boolean;
  loading: boolean;
}

export function useBootstrap(
  client: BootstrapFetcher = fetchBootstrap,
  pollIntervalMs = POLL_INTERVAL_MS,
): BootstrapPollingState {
  const [snapshot, setSnapshot] = useState<BootstrapDto | null>(null);
  const [lastSuccessfulRefresh, setLastSuccessfulRefresh] =
    useState<Date | null>(null);
  const [offline, setOffline] = useState(false);
  const [loading, setLoading] = useState(true);
  const failures = useRef(0);

  const poll = useCallback(async () => {
    try {
      const next = await client();
      setSnapshot(next);
      setLastSuccessfulRefresh(new Date());
      failures.current = 0;
      setOffline(false);
    } catch {
      failures.current += 1;
      setOffline(true);
    } finally {
      setLoading(false);
    }
  }, [client]);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const schedule = async () => {
      await poll();
      if (cancelled) return;
      const delay = Math.min(
        pollIntervalMs * Math.max(1, 2 ** failures.current),
        MAX_RETRY_INTERVAL_MS,
      );
      timer = setTimeout(schedule, delay);
    };
    void schedule();
    return () => {
      cancelled = true;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [poll, pollIntervalMs]);

  return { snapshot, lastSuccessfulRefresh, offline, loading };
}
