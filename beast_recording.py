"""Fail-open, byte-exact recording of a Beast Binary TCP stream."""

from __future__ import annotations

from datetime import timezone
import json
from pathlib import Path
import threading
import time

from recording import RecordingStatus, _utc_text


class BeastRecorder:
    """Write raw Beast bytes and sparse UTC/stream-offset timing checkpoints."""

    TIMING_VERSION = 1
    TIMING_INTERVAL_SECONDS = 1.0

    def __init__(
        self,
        session_dir,
        host,
        port,
        error_handler=None,
        monotonic=time.monotonic,
        binary_opener=None,
        timing_opener=None,
    ):
        self.session_dir = Path(session_dir)
        self.host = host
        self.port = port
        self.binary_path = self.session_dir / "beast.bin"
        self.timing_path = self.session_dir / "beast_timing.jsonl"
        self.error_handler = error_handler
        self.status = RecordingStatus.OFF
        self.error_message = None
        self.bytes_written = 0
        self.chunks_written = 0
        self._monotonic = monotonic
        self._binary = None
        self._timing = None
        self._closed = False
        self._error_reported = False
        self._last_checkpoint_monotonic = None
        self._last_checkpoint_offset = 0
        self._last_connection_id = None
        self._last_reception_utc = None
        self._last_reception_monotonic = None
        self._lock = threading.RLock()
        binary_opener = binary_opener or (lambda: self.binary_path.open("ab"))
        timing_opener = timing_opener or (
            lambda: self.timing_path.open("a", encoding="utf-8", newline="\n"))
        try:
            self._binary = binary_opener()
            self._timing = timing_opener()
            self.status = RecordingStatus.RECORDING
        except Exception as error:
            self._fail("open", error)

    def _fail(self, operation, error):
        if self.status == RecordingStatus.FAILED:
            return
        self.status = RecordingStatus.FAILED
        self.error_message = "Beast recorder {} failed: {}".format(operation, error)
        for output in (self._binary, self._timing):
            try:
                if output is not None:
                    output.close()
            except Exception:
                pass
        self._binary = None
        self._timing = None
        if self.error_handler is not None and not self._error_reported:
            self._error_reported = True
            try:
                self.error_handler(self.error_message)
            except Exception:
                pass

    def _write_checkpoint(self, reception_utc, reception_monotonic, connection_id):
        record = {
            "version": self.TIMING_VERSION,
            "host": self.host,
            "port": self.port,
            "byte_offset_after": self.bytes_written,
            "bytes_since_previous": self.bytes_written - self._last_checkpoint_offset,
            "reception_utc": _utc_text(reception_utc),
            "monotonic_seconds": reception_monotonic,
            "connection_id": connection_id,
        }
        self._timing.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._binary.flush()
        self._timing.flush()
        self._last_checkpoint_monotonic = reception_monotonic
        self._last_checkpoint_offset = self.bytes_written

    def record_chunk(self, chunk, reception_utc, reception_monotonic, connection_id):
        """Append one recv() chunk unchanged; return False after any recorder failure."""
        if not isinstance(chunk, bytes):
            raise TypeError("Beast chunks must be bytes")
        if not chunk:
            return True
        with self._lock:
            if self.status != RecordingStatus.RECORDING or self._closed:
                return False
            try:
                self._binary.write(chunk)
                self.bytes_written += len(chunk)
                self.chunks_written += 1
                self._last_reception_utc = reception_utc
                self._last_reception_monotonic = reception_monotonic
                checkpoint_due = (
                    self._last_checkpoint_monotonic is None
                    or connection_id != self._last_connection_id
                    or reception_monotonic - self._last_checkpoint_monotonic
                    >= self.TIMING_INTERVAL_SECONDS
                )
                self._last_connection_id = connection_id
                if checkpoint_due:
                    self._write_checkpoint(
                        reception_utc, reception_monotonic, connection_id)
                return True
            except Exception as error:
                self._fail("write", error)
                return False

    def flush_if_due(self):
        with self._lock:
            if (self.status != RecordingStatus.RECORDING or self._closed
                    or self._last_reception_monotonic is None
                    or self._last_checkpoint_offset == self.bytes_written):
                return False
            if (self._last_checkpoint_monotonic is not None
                    and self._monotonic() - self._last_checkpoint_monotonic
                    < self.TIMING_INTERVAL_SECONDS):
                return False
            try:
                self._write_checkpoint(
                    self._last_reception_utc,
                    self._last_reception_monotonic,
                    self._last_connection_id,
                )
                return True
            except Exception as error:
                self._fail("flush", error)
                return False

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self.status != RecordingStatus.RECORDING:
                return
            try:
                if (self._last_reception_monotonic is not None
                        and self._last_checkpoint_offset != self.bytes_written):
                    self._write_checkpoint(
                        self._last_reception_utc,
                        self._last_reception_monotonic,
                        self._last_connection_id,
                    )
                self._binary.flush()
                self._timing.flush()
                self._binary.close()
                self._timing.close()
            except Exception as error:
                self._fail("close", error)
            finally:
                self._binary = None
                self._timing = None
