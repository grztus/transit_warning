"""Fail-open recording of RAW ADS-B, SBS ADS-B and MLAT session streams."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import json
import os
from pathlib import Path
import threading
import time
import zipfile


class RecordingStatus(str, Enum):
    OFF = "OFF"
    RECORDING = "RECORDING"
    FAILED = "FAILED"


def _report_archive_error(error_handler, message):
    if error_handler is not None:
        try:
            error_handler(message)
        except Exception:
            pass


def _verify_stream_archive(path, expected_files=None):
    """Verify CRCs, member names and optional source sizes without loading files."""
    expected_files = list(expected_files or ())
    with zipfile.ZipFile(path, "r") as archive:
        members = archive.infolist()
        names = [member.filename for member in members]
        if len(names) not in (2, 3) or len(set(names)) != len(names):
            return False
        if (sum(name.startswith("adsb_") and name.endswith(".log") for name in names) != 1
                or sum(name.startswith("mlat_") and name.endswith(".log") for name in names) != 1
                or sum(name.startswith("raw_") and name.endswith(".log") for name in names)
                != len(names) - 2):
            return False
        if archive.testzip() is not None:
            return False
        if expected_files:
            expected = {path.name: path.stat().st_size for path in expected_files}
            actual = {member.filename: member.file_size for member in members}
            if actual != expected:
                return False
    return True


def archive_session(session_dir, delete_raw=False, error_handler=None):
    """Create and verify ``streams.zip`` while preserving raw logs on failure."""
    session_dir = Path(session_dir)
    archive_path = session_dir / "streams.zip"
    temporary_path = session_dir / "streams.zip.tmp"

    try:
        adsb_files = list(session_dir.glob("adsb_*.log"))
        mlat_files = list(session_dir.glob("mlat_*.log"))
        raw_adsb_files = list(session_dir.glob("raw_*.log"))
        raw_files = adsb_files + mlat_files + raw_adsb_files

        if archive_path.exists():
            if not _verify_stream_archive(
                    archive_path,
                    raw_files if len(raw_files) in (2, 3) else None):
                raise ValueError("existing streams.zip failed verification")
            if delete_raw:
                for raw_path in raw_files:
                    raw_path.unlink()
            return True

        if (len(adsb_files) != 1 or len(mlat_files) != 1
                or len(raw_adsb_files) > 1):
            raise ValueError(
                "session must contain one ADS-B, one MLAT and at most one RAW log")

        temporary_path.unlink(missing_ok=True)
        with temporary_path.open("w+b") as output:
            with zipfile.ZipFile(
                    output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for raw_path in raw_files:
                    archive.write(raw_path, arcname=raw_path.name)

        if not _verify_stream_archive(temporary_path, raw_files):
            raise ValueError("temporary streams archive failed verification")

        os.replace(temporary_path, archive_path)
        if delete_raw:
            for raw_path in raw_files:
                raw_path.unlink()
        return True
    except Exception as error:
        try:
            temporary_path.unlink(missing_ok=True)
        except Exception:
            pass
        _report_archive_error(
            error_handler, "Session archive failed: {}".format(error))
        return False


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
    """Own independent stream writers and the version 1 session manifest."""

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
        raw_port=None,
    ):
        self.session_start_utc = session_start_utc
        self.session_id = session_start_utc.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.base_dir = Path(base_dir)
        self.session_dir = self.base_dir / self.session_id
        self.manifest_path = self.session_dir / "manifest.json"
        self.adsb_port = adsb_port
        self.mlat_port = mlat_port
        self.raw_port = raw_port
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
        if raw_port is not None:
            self.writers[raw_port] = stream_writer_factory(
                self.session_dir / "raw_{}.log".format(raw_port),
                "RAW ADS-B", error_handler, monotonic=monotonic)
        self.write_manifest()

    @property
    def adsb_writer(self):
        return self.writers.get(self.adsb_port)

    @property
    def mlat_writer(self):
        return self.writers.get(self.mlat_port)

    @property
    def raw_writer(self):
        return self.writers.get(self.raw_port)

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

    def _stream_manifest(self, writer, port, semantics, timezone_name=None,
                         stream_format="sbs-basestation",
                         include_availability=False):
        if writer.status == RecordingStatus.FAILED:
            status = "failed"
        elif self._closed:
            status = "complete"
        else:
            status = "recording"
        result = {
            "file": writer.path.name,
            "port": port,
            "format": stream_format,
            "timestamp_semantics": semantics,
            "status": status,
            "line_count": writer.lines_written,
            "error": writer.error_message,
        }
        if timezone_name is not None:
            result["timestamp_timezone"] = timezone_name
        if include_availability:
            result["available"] = writer.lines_written > 0
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
        raw = self.raw_writer
        if raw is not None:
            result["raw"] = self._stream_manifest(
                raw, self.raw_port, "receiver-clock",
                stream_format="raw-mode-s-text",
                include_availability=True)
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
