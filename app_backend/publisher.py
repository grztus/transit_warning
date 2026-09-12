"""Bounded latest-state publication, never an event or lifecycle queue."""
import logging
import threading
import time


class PublicStatePublisher:
    def __init__(self, publish, interval=0.2):
        if interval <= 0:
            raise ValueError("positive publication interval required")
        self._publish = publish
        self._interval = interval
        self._condition = threading.Condition()
        self._generation = self._published_generation = 0
        self._closing = self._closed = False
        self._attempts = self._published = self._failures = self._coalesced = 0
        self._total_seconds = self._max_seconds = 0.0
        self._barrier_waits = self._barrier_failures = 0
        self._thread = threading.Thread(target=self._run, name="public-state-publisher", daemon=True)
        self._thread.start()

    def mark_dirty(self):
        with self._condition:
            if self._closing:
                return self._generation
            if self._generation > self._published_generation:
                self._coalesced += 1
            self._generation += 1
            self._condition.notify_all()
            return self._generation

    def flush(self, timeout=5.0):
        """Wait for changes known at entry, without building on the caller thread."""
        deadline = time.monotonic() + timeout
        with self._condition:
            target = self._generation
            if self._published_generation < target:
                self._barrier_waits += 1
            while self._published_generation < target:
                if threading.current_thread() is self._thread:
                    self._barrier_failures += 1
                    return False  # A callback cannot wait for its own acknowledgement.
                remaining = deadline - time.monotonic()
                if self._closed or remaining <= 0:
                    self._barrier_failures += 1
                    return False
                self._condition.wait(remaining)
            return True

    def snapshot(self):
        with self._condition:
            return dict(dirty_marks=self._generation, publish_attempts=self._attempts,
                        published=self._published, coalesced_marks=self._coalesced,
                        publish_failures=self._failures,
                        pending_dirty=self._generation > self._published_generation,
                        current_generation=self._generation,
                        published_generation=self._published_generation,
                        mean_publish_seconds=self._total_seconds / self._attempts if self._attempts else 0.0,
                        max_publish_seconds=self._max_seconds,
                        barrier_waits=self._barrier_waits,
                        barrier_failures=self._barrier_failures,
                        interval_seconds=self._interval,
                        worker_alive=self._thread.is_alive(),
                        shutdown_drained=(self._published_generation == self._generation)
                            if self._closed else None)

    def close(self):
        # Producers must stop first. One final attempt; no infinite failure retry.
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        self._thread.join()

    def _run(self):
        next_attempt = time.monotonic() + self._interval
        while True:
            with self._condition:
                while True:
                    dirty = self._generation > self._published_generation
                    if self._closing and not dirty:
                        self._closed = True
                        self._condition.notify_all()
                        return
                    delay = next_attempt - time.monotonic()
                    if dirty and delay <= 0:
                        generation = self._generation
                        final = self._closing
                        break
                    self._condition.wait(delay if dirty else None)
            started = time.monotonic()
            success = False
            try:
                self._publish()
                success = True
            except Exception:
                # Do not log exception payloads: they may contain private state.
                logging.getLogger(__name__).error("Public-state publication failed; latest state retained for retry")
            elapsed = time.monotonic() - started
            with self._condition:
                self._attempts += 1
                self._total_seconds += elapsed
                self._max_seconds = max(self._max_seconds, elapsed)
                if success:
                    self._published += 1
                    self._published_generation = generation
                else:
                    self._failures += 1
                next_attempt = max(started + self._interval, time.monotonic())
                self._condition.notify_all()
                if final:
                    self._closed = True
                    self._condition.notify_all()
                    return
