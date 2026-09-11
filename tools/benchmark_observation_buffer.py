"""Synthetic, non-gating retention benchmark and frozen pre-optimization oracle.

Run from the repository root: python -m tools.benchmark_observation_buffer
No recorder files, private observations, or benchmark output files are created.
"""
import datetime as dt
from collections import deque
from copy import deepcopy
import json
import statistics
import time

from transit_snapshot import (TransitSnapshotManager, BUFFER_RETENTION_SECONDS,
                              BUFFER_STALE_SECONDS, BUFFER_MAXLEN)


class LegacyObservationManager(TransitSnapshotManager):
    """Original retention algorithms, retained only as an equivalence oracle."""

    def record_observation(self, observation):
        try:
            item = deepcopy(observation)
            timestamp = item['timestamp_utc']
            icao = str(item['icao'])
            self._require_aware(timestamp, 'observation timestamp')
            with self._lock:
                latest = max(timestamp, self._buffer_last_seen.get(icao, timestamp))
                self._buffer_last_seen[icao] = latest
                existing = self._buffers.get(icao, ())
                cutoff = latest - dt.timedelta(seconds=BUFFER_RETENTION_SECONDS)
                retained = [sample for sample in existing if sample['timestamp_utc'] >= cutoff]
                if timestamp >= cutoff:
                    retained.append(item)
                self._buffers[icao] = deque(retained, maxlen=BUFFER_MAXLEN)
                self._cleanup_locked(latest)
            return True
        except Exception as error:
            self._fail(error)
            return False

    def _cleanup_locked(self, now_utc):
        recent_expired = [key for key, expires in self._recent_events.items() if expires < now_utc]
        for key in recent_expired:
            del self._recent_events[key]
        active_icaos = {key[0] for key in self._active}
        stale_before = now_utc - dt.timedelta(seconds=BUFFER_STALE_SECONDS)
        stale_icaos = [icao for icao, timestamp in self._buffer_last_seen.items()
                       if icao not in active_icaos and timestamp < stale_before]
        for icao in stale_icaos:
            self._buffers.pop(icao, None)
            self._buffer_last_seen.pop(icao, None)


BASE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def observation(icao, seconds, sequence=0):
    stamp = BASE + dt.timedelta(seconds=seconds)
    return dict(icao=icao, timestamp_utc=stamp, sequence=sequence, lat=0., lon=0.,
                altitude_m=10000., groundspeed=450., track=180., vertical_rate_fpm=0.,
                selected_altitude_ft=None, message_source='MSG', message_type='MSG,3',
                parameter_sources={'position': 'adsb', 'altitude': 'adsb'},
                source_timestamps_utc={'position': stamp, 'altitude': stamp},
                parameter_ages_seconds={'position': 0., 'altitude': 0.})


def prediction(icao='000000', seconds=31.):
    return dict(icao=icao, body='SUN', encounter_id='synthetic-' + icao,
                predicted_transit_utc=BASE + dt.timedelta(seconds=seconds),
                recorded_at_utc=BASE + dt.timedelta(seconds=seconds-1),
                separation_deg=.1, time2x_seconds=1., observer={})


def measure(factory, count):
    manager = factory(git_commit='benchmark')
    # 30 seconds at 10 accepted messages/s per aircraft.
    for tick in range(300):
        for index in range(count):
            manager.record_observation(observation(f'{index:06X}', tick / 10))
    samples = [observation(f'{index % count:06X}', 30 + (index // count) / 10, index)
               for index in range(count * 10)]
    start = time.perf_counter()
    for sample in samples:
        manager.record_observation(sample)
    record_us = (time.perf_counter() - start) * 1e6 / len(samples)
    now = BASE + dt.timedelta(seconds=31)
    start = time.perf_counter()
    for _ in range(200):
        manager.cleanup(now)
    cleanup_us = (time.perf_counter() - start) * 1e6 / 200
    manager.consider_prediction(prediction(seconds=32))
    event = next(iter(manager._active.values()))
    start = time.perf_counter()
    for _ in range(30):
        with manager._lock:
            manager._document(event, now, True, 'benchmark')
    extraction_us = (time.perf_counter() - start) * 1e6 / 30
    manager.invalidate_active_predictions()
    start = time.perf_counter()
    manager.cleanup(BASE + dt.timedelta(seconds=200))
    idle_cleanup_us = (time.perf_counter() - start) * 1e6
    return dict(record_us=record_us, cleanup_us=cleanup_us,
                extraction_us=extraction_us, idle_cleanup_us=idle_cleanup_us)


def main():
    for count in (30, 100, 300):
        result = {'aircraft': count, 'samples_per_aircraft': 300, 'median_of_runs': 3}
        for name, factory in (('before', LegacyObservationManager), ('after', TransitSnapshotManager)):
            runs = [measure(factory, count) for _ in range(3)]
            result[name] = {key: round(statistics.median(run[key] for run in runs), 2)
                            for key in runs[0]}
        print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
