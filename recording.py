"""Fail-open recording of aircraft session streams."""

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


def _verify_stream_archive(path, expected_files=None, expected_names=None):
    """Verify CRCs, exact member names and source sizes without loading files."""
    expected_files = list(expected_files or ())
    with zipfile.ZipFile(path, "r") as archive:
        members = archive.infolist()
        names = [member.filename for member in members]
        if len(set(names)) != len(names):
            return False
        if expected_names is not None and set(names) != set(expected_names):
            return False
        if expected_names is None:
            core_adsb = [name for name in names
                         if name.startswith("adsb_") and name.endswith(".log")]
            core_mlat = [name for name in names
                         if name.startswith("mlat_") and name.endswith(".log")]
            allowed = set(core_adsb + core_mlat)
            allowed.update(name for name in names
                           if name.startswith("raw_") and name.endswith(".log"))
            allowed.update(name for name in names
                           if name.startswith("mlat_beast_") and (
                               name.endswith(".bin") or name.endswith(".jsonl")))
            if len(core_adsb) != 1 or len(core_mlat) != 1 or allowed != set(names):
                return False
        if archive.testzip() is not None:
            return False
        if expected_files:
            expected = {path.name: path.stat().st_size for path in expected_files}
            actual = {member.filename: member.file_size for member in members}
            if actual != expected:
                return False
    return True


def _manifest_archive_members(session_dir):
    """Return manifest-declared archive names and existing loose paths."""
    manifest_path = session_dir / "manifest.json"
    if not manifest_path.is_file():
        return None, None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    names = []
    for section_name, keys, required in (
            ("adsb", ("file",), True),
            ("mlat", ("file",), True),
            ("raw", ("file", "events_file"), False),
            ("mlat_beast", ("file", "events_file"), False)):
        section = manifest.get(section_name)
        if section is None:
            if required:
                raise ValueError("manifest is missing {} stream".format(
                    section_name))
            continue
        for key in keys:
            name = section.get(key)
            if name:
                names.append(str(name))
            elif required:
                raise ValueError("manifest {} stream has no {}".format(
                    section_name, key))
    if len(names) != len(set(names)):
        raise ValueError("manifest declares duplicate stream files")
    paths = [session_dir / name for name in names]
    return names, paths


def _validate_manifest_recorded_counts(session_dir):
    """Check final loose-stream counts before they become canonical ZIP data."""
    manifest_path = session_dir / "manifest.json"
    if not manifest_path.is_file():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for section_name in ("adsb", "mlat", "raw"):
        section = manifest.get(section_name)
        if not section or not section.get("file"):
            continue
        path = session_dir / section["file"]
        with path.open("rb") as stream:
            actual_lines = sum(1 for _ in stream)
        if actual_lines != int(section.get("line_count", -1)):
            raise ValueError("{} line count does not match manifest".format(
                section_name))
        if section_name == "raw" and section.get("events_file"):
            events_path = session_dir / section["events_file"]
            with events_path.open("rb") as events:
                event_count = sum(1 for _ in events)
            if event_count != int(section.get(
                    "diagnostic_event_count", -1)):
                raise ValueError(
                    "raw diagnostic event count does not match manifest")

    section = manifest.get("mlat_beast")
    if not section or not section.get("file"):
        return
    binary_path = session_dir / section["file"]
    events_path = session_dir / section["events_file"]
    if binary_path.stat().st_size != int(section.get("bytes_written", -1)):
        raise ValueError("MLAT Beast byte count does not match manifest")
    expected_sequence = 1
    with events_path.open("r", encoding="utf-8") as events:
        for line in events:
            event = json.loads(line)
            if event.get("sequence") != expected_sequence:
                raise ValueError("MLAT Beast event sequence is invalid")
            expected_sequence += 1
    actual_frames = expected_sequence - 1
    if actual_frames != int(section.get("frames_recorded", -1)):
        raise ValueError("MLAT Beast frame count does not match manifest")
    tc19_updates = int(section.get("tc19_updates", -1))
    if tc19_updates < 0 or tc19_updates > actual_frames:
        raise ValueError("MLAT Beast TC19 count is invalid")


def archive_session(session_dir, delete_raw=False, error_handler=None):
    """Create and verify ``streams.zip`` while preserving raw logs on failure."""
    session_dir = Path(session_dir)
    archive_path = session_dir / "streams.zip"
    temporary_path = session_dir / "streams.zip.tmp"

    try:
        expected_names, manifest_files = _manifest_archive_members(session_dir)
        if manifest_files is None:
            adsb_files = list(session_dir.glob("adsb_*.log"))
            mlat_files = [path for path in session_dir.glob("mlat_*.log")
                          if not path.name.startswith("mlat_beast_")]
            raw_adsb_files = list(session_dir.glob("raw_*.log"))
            mlat_beast_files = (
                list(session_dir.glob("mlat_beast_*.bin"))
                + list(session_dir.glob("mlat_beast_*_events.jsonl")))
            raw_files = adsb_files + mlat_files + raw_adsb_files + mlat_beast_files
            expected_names = [path.name for path in raw_files]
            if ((raw_files or not archive_path.exists())
                    and (len(adsb_files) != 1 or len(mlat_files) != 1
                         or len(raw_adsb_files) > 1
                         or len(mlat_beast_files) not in (0, 2))):
                raise ValueError("session stream set is invalid")
        else:
            raw_files = manifest_files

        if archive_path.exists():
            existing_files = [path for path in raw_files if path.is_file()]
            if not _verify_stream_archive(
                    archive_path, existing_files,
                    expected_names if expected_names else None):
                raise ValueError("existing streams.zip failed verification")
            if delete_raw:
                for raw_path in existing_files:
                    raw_path.unlink()
            return True

        missing = [path.name for path in raw_files if not path.is_file()]
        if missing:
            raise ValueError(
                "manifest-declared stream files are missing: {}".format(
                    ", ".join(missing)))
        _validate_manifest_recorded_counts(session_dir)

        temporary_path.unlink(missing_ok=True)
        with temporary_path.open("w+b") as output:
            with zipfile.ZipFile(
                    output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for raw_path in raw_files:
                    archive.write(raw_path, arcname=raw_path.name)

        if not _verify_stream_archive(
                temporary_path, raw_files, expected_names):
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


class MlatBeastWriter:
    """Record exact Beast bytes plus parsed-frame receipt timing."""

    FLUSH_INTERVAL_SECONDS = 1.0
    EVENT_VERSION = 1

    def __init__(self, binary_path, events_path, label="MLAT Beast",
                 error_handler=None, monotonic=time.monotonic,
                 binary_opener=None, events_opener=None):
        self.binary_path = Path(binary_path)
        self.events_path = Path(events_path)
        self.label = label
        self.error_handler = error_handler
        self.status = RecordingStatus.OFF
        self.error_message = None
        self.bytes_written = 0
        self.frames_recorded = 0
        self.tc19_updates = 0
        self.files_initialized = False
        self._binary = None
        self._events = None
        self._closed = False
        self._dirty = False
        self._error_reported = False
        self._lock = threading.RLock()
        self._monotonic = monotonic
        self._last_flush_time = self._monotonic()
        binary_opener = binary_opener or (lambda: self.binary_path.open("wb"))
        events_opener = events_opener or (lambda: self.events_path.open(
            "w", encoding="utf-8", newline="\n"))
        try:
            self._binary = binary_opener()
            self._events = events_opener()
            self.files_initialized = True
            self.status = RecordingStatus.RECORDING
        except Exception as error:
            for output in (self._binary, self._events):
                try:
                    if output is not None:
                        output.close()
                except Exception:
                    pass
            self._binary = None
            self._events = None
            for path in (self.binary_path, self.events_path):
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
            self._fail("open", error)

    def _fail(self, operation, error):
        if self.status == RecordingStatus.FAILED:
            return
        self.status = RecordingStatus.FAILED
        self.error_message = "{} recorder {} failed: {}".format(
            self.label, operation, error)
        for output in (self._binary, self._events):
            try:
                if output is not None:
                    output.close()
            except Exception:
                pass
        self._binary = None
        self._events = None
        self._closed = True
        if self.error_handler is not None and not self._error_reported:
            self._error_reported = True
            try:
                self.error_handler(self.error_message)
            except Exception:
                pass

    def record_bytes(self, chunk):
        """Write the exact bytes returned by the production socket recv()."""
        with self._lock:
            if self.status != RecordingStatus.RECORDING or self._binary is None:
                return False
            try:
                chunk = bytes(chunk)
                self._binary.write(chunk)
                self.bytes_written += len(chunk)
                self._dirty = self._dirty or bool(chunk)
                self._flush_if_due_locked()
                return True
            except Exception as error:
                self._fail("binary write", error)
                return False

    def record_event(self, frame, received_at_utc, tc19_update=False):
        """Append one parsed Beast frame in decoder order with receipt UTC."""
        with self._lock:
            if self.status != RecordingStatus.RECORDING or self._events is None:
                return False
            try:
                sequence = self.frames_recorded + 1
                event = {
                    "version": self.EVENT_VERSION,
                    "sequence": sequence,
                    "received_at_utc": _utc_text(received_at_utc),
                    "frame_type": frame.frame_type,
                    "beast_timestamp": "{:012X}".format(
                        frame.beast_timestamp),
                    "signal": frame.signal,
                    "modes_hex": frame.modes.hex().upper(),
                }
                self._events.write(json.dumps(
                    event, separators=(",", ":")) + "\n")
                self.frames_recorded = sequence
                if tc19_update:
                    self.tc19_updates += 1
                self._dirty = True
                self._flush_if_due_locked()
                return True
            except Exception as error:
                self._fail("event write", error)
                return False

    def _flush_if_due_locked(self, force=False):
        if (self.status != RecordingStatus.RECORDING
                or self._binary is None or self._events is None
                or not self._dirty):
            return False
        now = self._monotonic()
        if not force and now - self._last_flush_time < self.FLUSH_INTERVAL_SECONDS:
            return False
        try:
            self._binary.flush()
            self._events.flush()
            self._dirty = False
            self._last_flush_time = now
            return True
        except Exception as error:
            self._fail("flush", error)
            return False

    def flush_if_due(self):
        with self._lock:
            return self._flush_if_due_locked()

    def close(self):
        with self._lock:
            if self._closed:
                return
            if self.status == RecordingStatus.RECORDING:
                self._flush_if_due_locked(force=True)
            if self.status == RecordingStatus.RECORDING:
                try:
                    self._binary.close()
                    self._events.close()
                except Exception as error:
                    self._fail("close", error)
            self._binary = None
            self._events = None
            self._closed = True


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
        mlat_beast_port=None,
        mlat_beast_writer_factory=MlatBeastWriter,
    ):
        self.session_start_utc = session_start_utc
        self.session_id = session_start_utc.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.base_dir = Path(base_dir)
        self.session_dir = self.base_dir / self.session_id
        self.manifest_path = self.session_dir / "manifest.json"
        self.adsb_port = adsb_port
        self.mlat_port = mlat_port
        self.raw_port = raw_port
        self.mlat_beast_port = mlat_beast_port
        self.adsb_timestamp_timezone = adsb_timestamp_timezone
        self.error_handler = error_handler
        self.manifest_error = None
        self._session_failed = False
        self._manifest_error_reported = False
        self._closed = False
        self.session_end_utc = None
        self._lock = threading.RLock()
        self.writers = {}
        self.mlat_beast_writer = None
        self.raw_events_writer = None

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
            self.raw_events_writer = stream_writer_factory(
                self.session_dir / "raw_{}_events.jsonl".format(raw_port),
                "RAW ADS-B diagnostics", error_handler,
                monotonic=monotonic)
        if mlat_beast_port is not None:
            self.mlat_beast_writer = mlat_beast_writer_factory(
                self.session_dir / "mlat_beast_{}.bin".format(
                    mlat_beast_port),
                self.session_dir / "mlat_beast_{}_events.jsonl".format(
                    mlat_beast_port),
                error_handler=error_handler,
                monotonic=monotonic)
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
            events = self.raw_events_writer
            result["raw"].update({
                "events_file": events.path.name if events is not None else None,
                "diagnostics_format": "raw-adsb-diagnostics-jsonl-v1",
                "diagnostic_event_count": (
                    events.lines_written if events is not None else 0),
                "diagnostics_status": (
                    "failed" if events is not None
                    and events.status == RecordingStatus.FAILED
                    else "complete" if events is not None and self._closed
                    else "recording" if events is not None else None),
                "diagnostics_error": (
                    events.error_message if events is not None else None),
            })
        mlat_beast = self.mlat_beast_writer
        if mlat_beast is not None:
            if mlat_beast.status == RecordingStatus.FAILED:
                status = "failed"
            elif self._closed:
                status = "complete"
            else:
                status = "recording"
            result["mlat_beast"] = {
                "file": (mlat_beast.binary_path.name
                         if mlat_beast.files_initialized else None),
                "events_file": (mlat_beast.events_path.name
                                if mlat_beast.files_initialized else None),
                "port": self.mlat_beast_port,
                "format": "beast-binary-synthetic-mlat",
                "timestamp_semantics": "magic-mlat-marker",
                "receipt_timing": (mlat_beast.events_path.name
                                   if mlat_beast.files_initialized else None),
                "receipt_timestamp_semantics": "production-receipt-utc",
                "status": status,
                "available": mlat_beast.bytes_written > 0,
                "bytes_written": mlat_beast.bytes_written,
                "frames_recorded": mlat_beast.frames_recorded,
                "tc19_updates": mlat_beast.tc19_updates,
                "error": mlat_beast.error_message,
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

    def record_mlat_beast_bytes(self, chunk):
        if self._closed or self.mlat_beast_writer is None:
            return False
        return self.mlat_beast_writer.record_bytes(chunk)

    def record_raw_diagnostic_event(self, event):
        if self._closed or self.raw_events_writer is None:
            return False
        try:
            line = json.dumps(event, separators=(",", ":")) + "\n"
        except Exception:
            return False
        return self.raw_events_writer.record_line(line)

    def record_mlat_beast_event(
            self, frame, received_at_utc, tc19_update=False):
        if self._closed or self.mlat_beast_writer is None:
            return False
        return self.mlat_beast_writer.record_event(
            frame, received_at_utc, tc19_update)

    def flush_if_due(self):
        """Flush dirty writers whose one-second deadline has elapsed."""
        for writer in self.writers.values():
            writer.flush_if_due()
        if self.raw_events_writer is not None:
            self.raw_events_writer.flush_if_due()
        if self.mlat_beast_writer is not None:
            self.mlat_beast_writer.flush_if_due()

    def close(self, session_end_utc=None):
        with self._lock:
            if self._closed:
                return
            for writer in self.writers.values():
                writer.close()
            if self.raw_events_writer is not None:
                self.raw_events_writer.close()
            if self.mlat_beast_writer is not None:
                self.mlat_beast_writer.close()
            try:
                if session_end_utc is not None:
                    _utc_text(session_end_utc)
                self.session_end_utc = session_end_utc
            except Exception as error:
                self._fail_session("finalization", error)
            self._closed = True
            self.write_manifest()
