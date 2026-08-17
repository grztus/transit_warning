"""Fail-open recording of raw ADS-B and MLAT session streams."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import json
import os
from pathlib import Path
import threading
import time


class RecordingStatus(str, Enum):
    OFF = "OFF"
    RECORDING = "RECORDING"
    FAILED = "FAILED"


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("recording timestamps must be timezone-aware UTC")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class StreamWriter:
    """Write one raw text stream without changing its contents."""

    FLUSH_INTERVAL_SECONDS = 1.0
    FLUSH_LINE_COUNT = 1000

    def __init__(
        self, path, label, error_handler=None, opener=None, monotonic=time.monotonic
    ):
        self.path = Path(path)
        self.label = label
        self.error_handler = error_handler
        self.status = RecordingStatus.OFF
        self.lines_written = 0
        self.error_message = None
        self._error_reported = False
        self._file = None
        self._closed = False
        self._lock = threading.RLock()
        self._opener = opener or self._open
        self._monotonic = monotonic
        self._last_flush_time = self._monotonic()
        self._lines_since_flush = 0
        try:
            self._file = self._opener()
            self.status = RecordingStatus.RECORDING
        except Exception as error:
            self._fail("open", error)

    def _open(self):
        return self.path.open("a", encoding="utf-8", newline="")

    def _fail(self, operation, error):
        if self.status == RecordingStatus.FAILED:
            return
        self.status = RecordingStatus.FAILED
        self.error_message = "{} recorder {} failed: {}".format(
            self.label, operation, error)
        try:
            if self._file is not None:
                self._file.close()
        except Exception:
            pass
        self._file = None
        self._closed = True
        if self.error_handler is not None and not self._error_reported:
            self._error_reported = True
            try:
                self.error_handler(self.error_message)
            except Exception:
                pass

    def record_line(self, line):
        """Write exactly the text supplied by ``readline()``."""
        with self._lock:
            if self.status != RecordingStatus.RECORDING or self._file is None:
                return False
            try:
                self._file.write(line)
                self.lines_written += 1
                self._lines_since_flush += 1
                now = self._monotonic()
                if (self._lines_since_flush >= self.FLUSH_LINE_COUNT
                        or now - self._last_flush_time >= self.FLUSH_INTERVAL_SECONDS):
                    if not self._flush_locked(now):
                        return False
                return True
            except Exception as error:
                self._fail("write", error)
                return False

    @property
    def lines_since_flush(self):
        return self._lines_since_flush

    def _flush_locked(self, now=None, force=False):
        if self.status != RecordingStatus.RECORDING or self._file is None:
            return False
        if not force and self._lines_since_flush == 0:
            return False
        try:
            self._file.flush()
            self._lines_since_flush = 0
            self._last_flush_time = self._monotonic() if now is None else now
            return True
        except Exception as error:
            self._fail("flush", error)
            return False

    def flush_if_due(self):
        with self._lock:
            now = self._monotonic()
            if (self._lines_since_flush == 0
                    or now - self._last_flush_time < self.FLUSH_INTERVAL_SECONDS):
                return False
            return self._flush_locked(now)

    def flush(self):
        with self._lock:
            return self._flush_locked()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._file is None:
                return
            try:
                if not self._flush_locked(force=True):
                    return
                self._file.close()
            except Exception as error:
                self._fail("close", error)
            finally:
                self._file = None


class SessionRecorder:
    """Own the two independent writers and the version 1 session manifest."""

    MANIFEST_VERSION = 1

    def __init__(
        self,
        session_start_utc,
        adsb_port,
        mlat_port,
        adsb_timestamp_timezone,
        base_dir=Path("recordings/sessions"),
        error_handler=None,
        stream_writer_factory=StreamWriter,
        monotonic=time.monotonic,
    ):
        self.session_start_utc = session_start_utc
        self.session_id = session_start_utc.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.base_dir = Path(base_dir)
        self.session_dir = self.base_dir / self.session_id
        self.manifest_path = self.session_dir / "manifest.json"
        self.adsb_port = adsb_port
        self.mlat_port = mlat_port
        self.adsb_timestamp_timezone = adsb_timestamp_timezone
        self.error_handler = error_handler
        self.manifest_error = None
        self._session_failed = False
        self._manifest_error_reported = False
        self._closed = False
        self.session_end_utc = None
        self._lock = threading.RLock()
        self.writers = {}

        try:
            _utc_text(session_start_utc)
            self.session_dir.mkdir(parents=True, exist_ok=False)
        except Exception as error:
            self._fail_session("initialization", error)
            return

        self.writers = {
            adsb_port: stream_writer_factory(
                self.session_dir / "adsb_{}.log".format(adsb_port),
                "ADS-B", error_handler, monotonic=monotonic),
            mlat_port: stream_writer_factory(
                self.session_dir / "mlat_{}.log".format(mlat_port),
                "MLAT", error_handler, monotonic=monotonic),
        }
        self.write_manifest()

    @property
    def adsb_writer(self):
        return self.writers.get(self.adsb_port)

    @property
    def mlat_writer(self):
        return self.writers.get(self.mlat_port)

    def _fail_session(self, operation, error):
        self._session_failed = True
        self.manifest_error = "Session recorder {} failed: {}".format(operation, error)
        if self.error_handler is not None and not self._manifest_error_reported:
            self._manifest_error_reported = True
            try:
                self.error_handler(self.manifest_error)
            except Exception:
                pass

    def _recording_status(self):
        statuses = [writer.status for writer in self.writers.values()]
        if not statuses or (self._closed and self._session_failed):
            return "failed"
        if not self._closed:
            return "recording"
        return "partial" if RecordingStatus.FAILED in statuses else "complete"

    def _stream_manifest(self, writer, port, semantics, timezone_name=None):
        if writer.status == RecordingStatus.FAILED:
            status = "failed"
        elif self._closed:
            status = "complete"
        else:
            status = "recording"
        result = {
            "file": writer.path.name,
            "port": port,
            "format": "sbs-basestation",
            "timestamp_semantics": semantics,
            "status": status,
            "line_count": writer.lines_written,
            "error": writer.error_message,
        }
        if timezone_name is not None:
            result["timestamp_timezone"] = timezone_name
        return result

    def manifest_data(self):
        adsb = self.adsb_writer
        mlat = self.mlat_writer
        result = {
            "version": self.MANIFEST_VERSION,
            "session_id": self.session_id,
            "session_start_utc": _utc_text(self.session_start_utc),
            "session_end_utc": (
                _utc_text(self.session_end_utc) if self.session_end_utc is not None else None),
            "recording_status": self._recording_status(),
            "adsb": self._stream_manifest(
                adsb, self.adsb_port, "local", self.adsb_timestamp_timezone) if adsb else None,
            "mlat": self._stream_manifest(
                mlat, self.mlat_port, "utc") if mlat else None,
        }
        return result

    def write_manifest(self):
        with self._lock:
            if not self.writers:
                return False
            temporary = self.manifest_path.with_suffix(".json.tmp")
            try:
                with temporary.open("w", encoding="utf-8", newline="\n") as output:
                    json.dump(self.manifest_data(), output, indent=2)
                    output.write("\n")
                os.replace(temporary, self.manifest_path)
                self.manifest_error = None
                return True
            except Exception as error:
                try:
                    temporary.unlink(missing_ok=True)
                except Exception:
                    pass
                self._fail_session("manifest write", error)
                return False

    def record_line(self, port, line):
        if self._closed:
            return False
        writer = self.writers.get(port)
        if writer is None:
            return False
        return writer.record_line(line)

    def flush_if_due(self):
        """Flush dirty writers whose one-second deadline has elapsed."""
        for writer in self.writers.values():
            writer.flush_if_due()

    def close(self, session_end_utc=None):
        with self._lock:
            if self._closed:
                return
            for writer in self.writers.values():
                writer.close()
            try:
                if session_end_utc is not None:
                    _utc_text(session_end_utc)
                self.session_end_utc = session_end_utc
            except Exception as error:
                self._fail_session("finalization", error)
            self._closed = True
            self.write_manifest()
