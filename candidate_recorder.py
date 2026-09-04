"""Candidate Auto-Recorder Phase 1: In-memory stream demultiplexing and pre-buffering.

This module provides the bounded in-memory pre-buffering foundation for the
Candidate Auto-Recorder. Buffering is keyed strictly by ICAO identity (not
encounter identity). It performs early filtering and demultiplexing of incoming
streams so that only reliably attributed data for known ICAOs is buffered.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
import os
from pathlib import Path
import queue
import re
import threading
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping

from authoritative_transit import (
    AuthoritativeTransition,
    AuthoritativeTransitionKind,
    PredictionGeometry,
)
from beast_intent import ESCAPE, BeastFrame, BeastFrameParser, modes_crc
from raw_adsb_track import extract_modes_hex


ICAO_HEX_RE = re.compile(r"^[0-9A-Fa-f]{6}$")


class StreamType(str, Enum):
    """Supported incoming stream types for candidate recording."""
    ADSB_SBS = "adsb_sbs"
    MLAT_SBS = "mlat_sbs"
    RAW_ADSB = "raw_adsb"
    MLAT_BEAST = "mlat_beast"


class CandidateEncounterOutcome(str, Enum):
    """Forensic outcome retained independently from physical capture state."""

    ACTIVE = "ACTIVE"
    WITHDRAWN = "WITHDRAWN"
    COMPLETED = "COMPLETED"


@dataclass(frozen=True)
class FullRecorderReference:
    """Frozen private reference to one active, fully covering FULL session."""

    session_id: str
    session_directory: Path
    session_start_utc: datetime
    streams: Mapping[str, Mapping[str, Any]]

    def __post_init__(self):
        frozen = {
            str(name): MappingProxyType(dict(details))
            for name, details in self.streams.items()
        }
        object.__setattr__(self, "streams", MappingProxyType(frozen))


@dataclass(frozen=True)
class CandidateEncounterState:
    """One triggered authoritative encounter and its immutable window state."""

    encounter_id: str
    observer_epoch: int
    icao: str
    body: str
    encounter_generation: int
    triggered_at_utc: datetime
    prebuffer_start_utc: datetime
    requested_end_time_utc: datetime
    required_end_time_utc: datetime
    hard_end_time_utc: datetime
    hard_ceiling_seconds: float
    hard_ceiling_applied: bool
    trigger_prediction: object
    latest_prediction: object
    outcome: CandidateEncounterOutcome
    last_update_utc: datetime
    completed_at_utc: datetime | None = None

    @property
    def unfinished(self) -> bool:
        return self.completed_at_utc is None


@dataclass(frozen=True)
class CandidateIcaoCaptureState:
    """Conceptual shared physical coverage for all unfinished ICAO encounters."""

    icao: str
    prebuffer_start_utc: datetime
    requested_capture_until_utc: datetime
    capture_until_utc: datetime
    hard_ceiling_applied: bool
    encounter_ids: tuple[str, ...]


class CandidateEncounterManager:
    """Dormant Phase 2 authoritative gating and in-memory capture planning.

    This manager performs no stream reads and no filesystem I/O.  It consumes
    the stable encounter identity created by ``AuthoritativeTransitLifecycle``.
    Physical capture remains a per-ICAO plan while forensic state remains one
    record per authoritative encounter.
    """

    DEFAULT_TRIGGER_HORIZON_SECONDS = 300.0
    DEFAULT_TRIGGER_SEPARATION_DEG = 2.0
    DEFAULT_POST_T0_SECONDS = 180.0
    DEFAULT_MAX_CAPTURE_DURATION_SECONDS = 1800.0

    def __init__(self, pre_buffer=None,
                 trigger_horizon_seconds=DEFAULT_TRIGGER_HORIZON_SECONDS,
                 trigger_separation_deg=DEFAULT_TRIGGER_SEPARATION_DEG,
                 post_t0_seconds=DEFAULT_POST_T0_SECONDS,
                 max_capture_duration_seconds=(
                     DEFAULT_MAX_CAPTURE_DURATION_SECONDS)):
        self.pre_buffer = pre_buffer
        self.trigger_horizon_seconds = float(trigger_horizon_seconds)
        self.trigger_separation_deg = float(trigger_separation_deg)
        self.post_t0_seconds = float(post_t0_seconds)
        self.max_capture_duration_seconds = float(
            max_capture_duration_seconds)
        if self.trigger_horizon_seconds <= 0:
            raise ValueError("trigger horizon must be positive")
        if self.trigger_separation_deg < 0:
            raise ValueError("trigger separation must be non-negative")
        if self.post_t0_seconds < 0:
            raise ValueError("post-T0 duration must be non-negative")
        if self.max_capture_duration_seconds <= 0:
            raise ValueError("maximum capture duration must be positive")
        self._encounters: dict[str, CandidateEncounterState] = {}
        self._captures: dict[str, CandidateIcaoCaptureState] = {}
        self._lock = threading.RLock()
        self.last_error: str | None = None

    def process_transition(self, transition, now_utc):
        """Consume one authoritative transition without propagating failures."""
        try:
            now = validate_utc_datetime(now_utc)
            if not isinstance(transition, AuthoritativeTransition):
                return None
            prediction = transition.prediction
            encounter_id = (str(prediction.encounter_id)
                            if prediction is not None else None)
            with self._lock:
                existing = self._encounters.get(encounter_id)
                if transition.kind == AuthoritativeTransitionKind.WITHDRAWN:
                    if existing is None:
                        return None
                    updated = self._replace_encounter(
                        existing,
                        latest_prediction=prediction,
                        outcome=CandidateEncounterOutcome.WITHDRAWN,
                        last_update_utc=now)
                    self._encounters[encounter_id] = updated
                    self._rebuild_capture_locked(updated.icao)
                    return updated
                if transition.kind not in (
                        AuthoritativeTransitionKind.OPENED,
                        AuthoritativeTransitionKind.UPDATED):
                    return existing
                if prediction is None or not self._is_true_2d_interior(prediction):
                    return existing
                if existing is None:
                    if not self._passes_trigger_gate(prediction, now):
                        return None
                    prebuffer_seconds = (
                        self.pre_buffer.buffer_duration_seconds
                        if self.pre_buffer is not None
                        else CandidatePreBuffer.DEFAULT_BUFFER_DURATION_SECONDS)
                    requested_end = prediction.predicted_transit_utc + timedelta(
                        seconds=self.post_t0_seconds)
                    hard_end = now + timedelta(
                        seconds=self.max_capture_duration_seconds)
                    required_end = min(
                        requested_end, hard_end)
                    existing = CandidateEncounterState(
                        encounter_id=encounter_id,
                        observer_epoch=int(prediction.observer_epoch),
                        icao=str(prediction.icao).upper(),
                        body=str(prediction.body).upper(),
                        encounter_generation=int(
                            prediction.encounter_generation),
                        triggered_at_utc=now,
                        prebuffer_start_utc=now - timedelta(
                            seconds=prebuffer_seconds),
                        requested_end_time_utc=requested_end,
                        required_end_time_utc=required_end,
                        hard_end_time_utc=hard_end,
                        hard_ceiling_seconds=self.max_capture_duration_seconds,
                        hard_ceiling_applied=requested_end > hard_end,
                        trigger_prediction=prediction,
                        latest_prediction=prediction,
                        outcome=CandidateEncounterOutcome.ACTIVE,
                        last_update_utc=now)
                elif existing.unfinished:
                    proposed_end = prediction.predicted_transit_utc + timedelta(
                        seconds=self.post_t0_seconds)
                    hard_end = existing.triggered_at_utc + timedelta(
                        seconds=self.max_capture_duration_seconds)
                    requested_end = max(
                        existing.requested_end_time_utc, proposed_end)
                    existing = self._replace_encounter(
                        existing,
                        requested_end_time_utc=requested_end,
                        required_end_time_utc=min(requested_end, hard_end),
                        hard_ceiling_applied=requested_end > hard_end,
                        latest_prediction=prediction,
                        last_update_utc=now)
                self._encounters[encounter_id] = existing
                self._rebuild_capture_locked(existing.icao)
                return existing
        except Exception as error:
            self.last_error = str(error)
            return None

    def complete_due(self, now_utc):
        """Finish elapsed forensic windows and update shared capture plans."""
        try:
            now = validate_utc_datetime(now_utc)
            completed = []
            affected_icaos = set()
            with self._lock:
                for encounter_id, state in tuple(self._encounters.items()):
                    if (state.unfinished
                            and now >= state.required_end_time_utc):
                        outcome = (state.outcome
                                   if state.outcome == CandidateEncounterOutcome.WITHDRAWN
                                   else CandidateEncounterOutcome.COMPLETED)
                        state = self._replace_encounter(
                            state, outcome=outcome, completed_at_utc=now,
                            last_update_utc=now)
                        self._encounters[encounter_id] = state
                        completed.append(state)
                        affected_icaos.add(state.icao)
                for icao in affected_icaos:
                    self._rebuild_capture_locked(icao)
            return tuple(completed)
        except Exception as error:
            self.last_error = str(error)
            return ()

    def encounter(self, encounter_id):
        with self._lock:
            return self._encounters.get(str(encounter_id))

    def encounters_for_icao(self, icao):
        normalized = normalize_icao(icao)
        if normalized is None:
            return ()
        with self._lock:
            return tuple(state for state in self._encounters.values()
                         if state.icao == normalized)

    def capture_state(self, icao):
        normalized = normalize_icao(icao)
        if normalized is None:
            return None
        with self._lock:
            return self._captures.get(normalized)

    def _passes_trigger_gate(self, prediction, now):
        seconds = (prediction.predicted_transit_utc - now).total_seconds()
        separation = float(prediction.separation_deg)
        return (0.0 < seconds <= self.trigger_horizon_seconds
                and 0.0 <= separation <= self.trigger_separation_deg)

    @staticmethod
    def _is_true_2d_interior(prediction):
        return (str(prediction.model).upper() == PredictionGeometry.TRUE_2D.value
                and str(prediction.boundary_status).upper() == "INTERIOR")

    @staticmethod
    def _replace_encounter(state, **changes):
        values = dict(state.__dict__)
        values.update(changes)
        return CandidateEncounterState(**values)

    def _rebuild_capture_locked(self, icao):
        unfinished = [state for state in self._encounters.values()
                      if state.icao == icao and state.unfinished]
        if not unfinished:
            self._captures.pop(icao, None)
            return
        requested_until = max(
            state.requested_end_time_utc for state in unfinished)
        applied_until = max(
            state.required_end_time_utc for state in unfinished)
        self._captures[icao] = CandidateIcaoCaptureState(
            icao=icao,
            prebuffer_start_utc=min(
                state.prebuffer_start_utc for state in unfinished),
            requested_capture_until_utc=requested_until,
            capture_until_utc=applied_until,
            hard_ceiling_applied=requested_until > applied_until,
            encounter_ids=tuple(sorted(
                state.encounter_id for state in unfinished)))


@dataclass(frozen=True)
class CandidateStreamRecord:
    """One candidate stream message or frame retained in the pre-buffer.

    Attributes:
        stream_type: Category of the stream (ADS-B SBS, MLAT SBS, RAW, or MLAT Beast).
        icao: 6-character uppercase hex ICAO address.
        received_at_utc: Timezone-aware UTC timestamp when the record was received.
        raw_data: Original stream representation (text line for SBS/RAW, wire bytes for Beast).
        metadata: Optional read-only diagnostic or decoding attributes.
    """
    stream_type: StreamType
    icao: str
    received_at_utc: datetime
    raw_data: str | bytes
    metadata: Mapping[str, Any] | None = None
    sequence_id: int = 0


def validate_utc_datetime(dt: datetime) -> datetime:
    """Validate that a datetime is a timezone-aware UTC timestamp.

    Raises TypeError if dt is not a datetime.
    Raises ValueError if dt is naive or has a non-UTC offset.
    """
    if not isinstance(dt, datetime):
        raise TypeError("Timestamp must be a datetime instance")
    if dt.tzinfo is None or dt.utcoffset() != timezone.utc.utcoffset(dt):
        raise ValueError("Recording timestamps must be timezone-aware UTC")
    return dt


def normalize_icao(raw_icao: str | None) -> str | None:
    """Validate and normalize a 24-bit Mode-S ICAO hex address."""
    if not raw_icao:
        return None
    raw = str(raw_icao).strip().upper()
    return raw if ICAO_HEX_RE.fullmatch(raw) else None


def attribute_sbs_icao(line: str) -> str | None:
    """Extract and validate the ICAO identity from an SBS BaseStation text line."""
    if not isinstance(line, str):
        return None
    parts = line.strip().split(",")
    if len(parts) < 5:
        return None
    return normalize_icao(parts[4])


def attribute_raw_adsb(line: str) -> tuple[str | None, bytes | None]:
    """Validate a port 30002 RAW line and return (icao, message_bytes) if attributable.

    Strict protocol policy:
    1. Valid 112-bit AVR frame format via extract_modes_hex (14 bytes).
    2. Zero Mode-S CRC remainder (modes_crc == 0).
    3. Authoritative Downlink Format:
       - DF17 (ADS-B Extended Squitter)
       - DF18 with CF==2 (Synthetic MLAT / Fine Format ADS-R)
    All other DFs (including DF18 CF!=2, DF11, DF19, and AP-overlaid frames)
    are strictly rejected.
    """
    hex_payload = extract_modes_hex(line)
    if hex_payload is None:
        return None, None
    try:
        message = bytes.fromhex(hex_payload)
    except ValueError:
        return None, None
    if len(message) != 14 or modes_crc(message) != 0:
        return None, None
    df = message[0] >> 3
    if df == 17:
        return message[1:4].hex().upper(), message
    if df == 18:
        cf = message[0] & 7
        if cf == 2:
            return message[1:4].hex().upper(), message
    return None, None


def attribute_beast_frame(frame: BeastFrame) -> str | None:
    """Validate a parsed Beast frame and return the ICAO address if reliably attributable.

    Strict protocol policy:
    - Frame type must be 0x33 (14-byte Mode-S).
    - CRC must be 0 (modes_crc == 0).
    - Authoritative Downlink Format:
      - DF17 (ADS-B Extended Squitter)
      - DF18 with CF==2 (Synthetic MLAT)
    Frame types 0x31 (Mode A/C), 0x32 (7-byte Mode-S), DF18 CF!=2, DF19, and
    AP-overlaid frames are strictly rejected.
    """
    if not isinstance(frame, BeastFrame):
        return None
    if frame.frame_type != 0x33:
        return None
    if len(frame.modes) != 14 or modes_crc(frame.modes) != 0:
        return None
    df = frame.modes[0] >> 3
    if df == 17:
        return frame.modes[1:4].hex().upper()
    if df == 18:
        cf = frame.modes[0] & 7
        if cf == 2:
            return frame.modes[1:4].hex().upper()
    return None


def encode_beast_wire(frame: BeastFrame) -> bytes:
    """Reconstruct canonical escaped Beast binary wire bytes for one frame."""
    timestamp = frame.beast_timestamp & 0xFFFFFFFFFFFF
    signal = frame.signal & 0xFF
    payload = (
        timestamp.to_bytes(6, "big")
        + bytes((signal,))
        + bytes(frame.modes)
    )
    return bytes((ESCAPE, frame.frame_type)) + payload.replace(
        bytes((ESCAPE,)), bytes((ESCAPE, ESCAPE))
    )


class IcaoStreamBuffer:
    """In-memory buffer holding stream records for a single ICAO."""

    def __init__(self, icao: str, max_records: int = 10000):
        self.icao = icao
        self.max_records = max(1, int(max_records))
        self.records: deque[CandidateStreamRecord] = deque()
        self.last_update_utc: datetime = datetime.now(timezone.utc)

    def append(self, record: CandidateStreamRecord) -> None:
        self.records.append(record)
        while len(self.records) > self.max_records:
            self.records.popleft()
        if record.received_at_utc > self.last_update_utc:
            self.last_update_utc = record.received_at_utc

    def prune(self, cutoff_utc: datetime) -> int:
        """Discard records older than cutoff_utc without assuming sorted order."""
        original_len = len(self.records)
        self.records = deque(
            r for r in self.records if r.received_at_utc >= cutoff_utc
        )
        return original_len - len(self.records)

    @property
    def is_empty(self) -> bool:
        return len(self.records) == 0

    def __len__(self) -> int:
        return len(self.records)


class CandidatePreBuffer:
    """Bounded, thread-safe pre-buffer demultiplexing streams by ICAO.

    Cleanup Policy:
    1. Time Window Pruning: Records older than ``buffer_duration_seconds``
       (default ~60 seconds) are dropped on pruning.
    2. Out-of-Order Robustness: Pruning filters all records against the cutoff
       without assuming monotonic insertion ordering. Returned record queries
       are guaranteed chronologically sorted.
    3. Stale ICAO Cleanup: Any ICAO whose buffer becomes empty after pruning
       is evicted from the internal dictionary so memory does not leak.
    4. Capacity Bounds & Prune-Before-Evict:
       - Each ICAO has a bounded record capacity (``max_records_per_icao``).
       - Total distinct ICAO entries are bounded (``max_icaos``).
       - When max_icaos is reached, stale buffers are pruned first; an active
         buffer is evicted only if capacity is still genuinely exhausted.
    5. Clock Integrity & Input Sanitization:
       - Pruning lifecycle is tied to runtime clock / monotonic interval.
       - Timestamps must be timezone-aware UTC; naive datetimes are rejected.
       - Erroneous future timestamps beyond ``max_future_skew_seconds`` are
         rejected to protect buffer health.
       - Retained metadata is read-only.
    """

    DEFAULT_BUFFER_DURATION_SECONDS = 60.0
    DEFAULT_MAX_RECORDS_PER_ICAO = 10000
    DEFAULT_MAX_ICAOS = 2048
    DEFAULT_MAX_FUTURE_SKEW_SECONDS = 300.0
    MIN_PRUNE_INTERVAL_SECONDS = 1.0

    def __init__(
        self,
        buffer_duration_seconds: float = DEFAULT_BUFFER_DURATION_SECONDS,
        max_records_per_icao: int = DEFAULT_MAX_RECORDS_PER_ICAO,
        max_icaos: int = DEFAULT_MAX_ICAOS,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        max_future_skew_seconds: float = DEFAULT_MAX_FUTURE_SKEW_SECONDS,
        record_sink: Callable[[CandidateStreamRecord], None] | None = None,
    ):
        self.buffer_duration_seconds = max(1.0, float(buffer_duration_seconds))
        self.max_records_per_icao = max(1, int(max_records_per_icao))
        self.max_icaos = max(1, int(max_icaos))
        self.max_future_skew_seconds = max(0.0, float(max_future_skew_seconds))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._buffers: dict[str, IcaoStreamBuffer] = {}
        self._beast_parser = BeastFrameParser()
        self._lock = threading.RLock()
        self._last_prune_mono: float | None = None
        self._record_sink = record_sink
        self._next_sequence_id = 1

    def _with_sequence_locked(self, record):
        sequenced = replace(record, sequence_id=self._next_sequence_id)
        self._next_sequence_id += 1
        return sequenced

    def set_record_sink(self, sink):
        """Attach an optional fail-open observer for newly admitted records."""
        with self._lock:
            self._record_sink = sink

    def _notify_record_sink(self, record):
        try:
            sink = self._record_sink
            if sink is not None:
                sink(record)
        except Exception:
            pass

    def _resolve_received_at(self, received_at_utc: datetime | None) -> datetime | None:
        current_time = validate_utc_datetime(self._clock())
        if received_at_utc is None:
            return current_time
        candidate = validate_utc_datetime(received_at_utc)
        if (candidate - current_time).total_seconds() > self.max_future_skew_seconds:
            return None
        return candidate

    def _get_or_create_buffer(self, icao: str, now_utc: datetime) -> IcaoStreamBuffer:
        buf = self._buffers.get(icao)
        if buf is None:
            if len(self._buffers) >= self.max_icaos:
                self._prune_locked(now_utc)
                if len(self._buffers) >= self.max_icaos:
                    self._evict_oldest_locked()
            buf = IcaoStreamBuffer(icao, max_records=self.max_records_per_icao)
            self._buffers[icao] = buf
        return buf

    def _evict_oldest_locked(self) -> None:
        if not self._buffers:
            return
        oldest_icao = min(
            self._buffers.keys(),
            key=lambda k: self._buffers[k].last_update_utc,
        )
        del self._buffers[oldest_icao]

    def _auto_prune_if_due_locked(self, current_utc: datetime) -> None:
        now_mono = self._monotonic()
        if (
            self._last_prune_mono is None
            or (now_mono - self._last_prune_mono) >= self.MIN_PRUNE_INTERVAL_SECONDS
            or (now_mono - self._last_prune_mono) < 0
        ):
            self._prune_locked(current_utc)
            self._last_prune_mono = now_mono

    def _prune_locked(self, now_utc: datetime) -> int:
        cutoff_utc = now_utc - timedelta(seconds=self.buffer_duration_seconds)
        total_pruned = 0
        stale_icaos = []
        for icao, buf in self._buffers.items():
            total_pruned += buf.prune(cutoff_utc)
            if buf.is_empty:
                stale_icaos.append(icao)
        for icao in stale_icaos:
            del self._buffers[icao]
        return total_pruned

    def prune(self, now_utc: datetime | None = None) -> int:
        """Prune records older than the buffer duration and evict stale ICAOs."""
        now = (
            validate_utc_datetime(now_utc)
            if now_utc is not None
            else validate_utc_datetime(self._clock())
        )
        with self._lock:
            pruned = self._prune_locked(now)
            self._last_prune_mono = self._monotonic()
            return pruned

    def feed_adsb_sbs(
        self, line: str, received_at_utc: datetime | None = None
    ) -> bool:
        """Demux and buffer one ADS-B BaseStation SBS line."""
        icao = attribute_sbs_icao(line)
        if icao is None:
            return False
        now = self._resolve_received_at(received_at_utc)
        if now is None:
            return False
        current_time = validate_utc_datetime(self._clock())
        record = CandidateStreamRecord(
            stream_type=StreamType.ADSB_SBS,
            icao=icao,
            received_at_utc=now,
            raw_data=line,
        )
        with self._lock:
            record = self._with_sequence_locked(record)
            self._auto_prune_if_due_locked(current_time)
            buf = self._get_or_create_buffer(icao, current_time)
            buf.append(record)
        self._notify_record_sink(record)
        return True

    def feed_mlat_sbs(
        self, line: str, received_at_utc: datetime | None = None
    ) -> bool:
        """Demux and buffer one MLAT BaseStation SBS line."""
        icao = attribute_sbs_icao(line)
        if icao is None:
            return False
        now = self._resolve_received_at(received_at_utc)
        if now is None:
            return False
        current_time = validate_utc_datetime(self._clock())
        record = CandidateStreamRecord(
            stream_type=StreamType.MLAT_SBS,
            icao=icao,
            received_at_utc=now,
            raw_data=line,
        )
        with self._lock:
            record = self._with_sequence_locked(record)
            self._auto_prune_if_due_locked(current_time)
            buf = self._get_or_create_buffer(icao, current_time)
            buf.append(record)
        self._notify_record_sink(record)
        return True

    def feed_raw_adsb(
        self, line: str, received_at_utc: datetime | None = None
    ) -> bool:
        """Demux and buffer one port 30002 RAW Mode-S line if attributable."""
        icao, message = attribute_raw_adsb(line)
        if icao is None or message is None:
            return False
        now = self._resolve_received_at(received_at_utc)
        if now is None:
            return False
        current_time = validate_utc_datetime(self._clock())
        record = CandidateStreamRecord(
            stream_type=StreamType.RAW_ADSB,
            icao=icao,
            received_at_utc=now,
            raw_data=line,
            metadata=MappingProxyType({"modes_hex": message.hex().upper()}),
        )
        with self._lock:
            record = self._with_sequence_locked(record)
            self._auto_prune_if_due_locked(current_time)
            buf = self._get_or_create_buffer(icao, current_time)
            buf.append(record)
        self._notify_record_sink(record)
        return True

    def feed_mlat_beast_frame(
        self, frame: BeastFrame, received_at_utc: datetime | None = None
    ) -> bool:
        """Demux and buffer one decoded BeastFrame if attributable."""
        icao = attribute_beast_frame(frame)
        if icao is None:
            return False
        now = self._resolve_received_at(received_at_utc)
        if now is None:
            return False
        current_time = validate_utc_datetime(self._clock())
        wire_bytes = encode_beast_wire(frame)
        record = CandidateStreamRecord(
            stream_type=StreamType.MLAT_BEAST,
            icao=icao,
            received_at_utc=now,
            raw_data=wire_bytes,
            metadata=MappingProxyType({
                "frame_type": frame.frame_type,
                "beast_timestamp": "{:012X}".format(
                    frame.beast_timestamp & 0xFFFFFFFFFFFF
                ),
                "signal": frame.signal & 0xFF,
                "modes_hex": frame.modes.hex().upper(),
            }),
        )
        with self._lock:
            record = self._with_sequence_locked(record)
            self._auto_prune_if_due_locked(current_time)
            buf = self._get_or_create_buffer(icao, current_time)
            buf.append(record)
        self._notify_record_sink(record)
        return True

    def feed_mlat_beast_chunk(
        self, chunk: bytes, received_at_utc: datetime | None = None
    ) -> list[str]:
        """Process incoming raw Beast TCP bytes across arbitrary boundaries.

        Decodes complete frames, attributes them, and retains attributable
        frames in per-ICAO buffers. Incomplete chunks or unattributable
        frames are not retained in any pre-buffer.
        """
        now = self._resolve_received_at(received_at_utc)
        if now is None:
            return []
        current_time = validate_utc_datetime(self._clock())
        attributed_icaos: list[str] = []
        admitted_records = []
        with self._lock:
            frames = self._beast_parser.feed(chunk)
            for frame in frames:
                icao = attribute_beast_frame(frame)
                if icao is not None:
                    wire_bytes = encode_beast_wire(frame)
                    record = CandidateStreamRecord(
                        stream_type=StreamType.MLAT_BEAST,
                        icao=icao,
                        received_at_utc=now,
                        raw_data=wire_bytes,
                        metadata=MappingProxyType({
                            "frame_type": frame.frame_type,
                            "beast_timestamp": "{:012X}".format(
                                frame.beast_timestamp & 0xFFFFFFFFFFFF
                            ),
                            "signal": frame.signal & 0xFF,
                            "modes_hex": frame.modes.hex().upper(),
                        }),
                    )
                    record = self._with_sequence_locked(record)
                    self._auto_prune_if_due_locked(current_time)
                    buf = self._get_or_create_buffer(icao, current_time)
                    buf.append(record)
                    attributed_icaos.append(icao)
                    admitted_records.append(record)
        for record in admitted_records:
            self._notify_record_sink(record)
        return attributed_icaos

    def get_records(self, icao: str) -> list[CandidateStreamRecord]:
        """Return a copy of all buffered records for an ICAO in chronological order."""
        normalized = normalize_icao(icao)
        if normalized is None:
            return []
        with self._lock:
            buf = self._buffers.get(normalized)
            if buf is None:
                return []
            return sorted(buf.records, key=lambda r: r.received_at_utc)

    def get_records_since(
        self, icao: str, since_utc: datetime
    ) -> list[CandidateStreamRecord]:
        """Return buffered records for an ICAO received at or after since_utc."""
        normalized = normalize_icao(icao)
        if normalized is None:
            return []
        since = validate_utc_datetime(since_utc)
        with self._lock:
            buf = self._buffers.get(normalized)
            if buf is None:
                return []
            return sorted(
                (r for r in buf.records if r.received_at_utc >= since),
                key=lambda r: r.received_at_utc,
            )

    def has_icao(self, icao: str) -> bool:
        """Whether there are active buffered records for the ICAO."""
        normalized = normalize_icao(icao)
        if normalized is None:
            return False
        with self._lock:
            buf = self._buffers.get(normalized)
            return buf is not None and not buf.is_empty

    def tracked_icaos(self) -> list[str]:
        """Return a sorted list of all ICAOs currently having buffered records."""
        with self._lock:
            return sorted(k for k, v in self._buffers.items() if not v.is_empty)

    def record_count(self, icao: str | None = None) -> int:
        """Return record count for a specific ICAO, or total across all ICAOs."""
        with self._lock:
            if icao is not None:
                normalized = normalize_icao(icao)
                if normalized is None:
                    return 0
                buf = self._buffers.get(normalized)
                return len(buf) if buf is not None else 0
            return sum(len(buf) for buf in self._buffers.values())

    def clear(self, icao: str | None = None) -> None:
        """Clear buffered records for a specific ICAO, or all if None."""
        with self._lock:
            if icao is not None:
                normalized = normalize_icao(icao)
                if normalized is not None and normalized in self._buffers:
                    del self._buffers[normalized]
            else:
                self._buffers.clear()


@dataclass
class _CandidateCapture:
    capture_id: str
    icao: str
    directory: Path
    started_at_utc: datetime
    capture_until_utc: datetime
    requested_capture_until_utc: datetime
    hard_ceiling_seconds: float
    hard_ceiling_applied: bool
    encounter_ids: set[str]
    streams: dict
    seen_records: set
    closed: bool = False
    streams_closed: bool = False
    degraded: bool = False
    degradation_reasons: set[str] = None
    recovered_unindexed_bytes: dict = None
    collision_index: int = 0

    def __post_init__(self):
        if self.degradation_reasons is None:
            self.degradation_reasons = set()
        if self.recovered_unindexed_bytes is None:
            self.recovered_unindexed_bytes = {}


class CandidateBundleStore:
    """Private per-ICAO stream capture with per-encounter manifests.

    The store owns no sockets or parsers. It receives only records already
    admitted by ``CandidatePreBuffer`` and is deliberately fail-open to its
    caller. A single physical capture is shared by overlapping encounters for
    one ICAO.
    """

    STREAM_FILES = {
        StreamType.ADSB_SBS: "adsb_sbs.log",
        StreamType.MLAT_SBS: "mlat_sbs.log",
        StreamType.RAW_ADSB: "raw_adsb.log",
        StreamType.MLAT_BEAST: "mlat_beast.bin",
    }

    def __init__(self, root_directory, pre_buffer,
                 full_recorder_reference=None):
        self.root_directory = Path(root_directory)
        self.pre_buffer = pre_buffer
        self.full_recorder_reference = full_recorder_reference
        self._captures = {}
        self._encounters = {}
        self._encounter_contexts = {}
        self._encounter_captures = {}
        self._completed_encounter_ids = set()
        self._reference_directories = {}
        self._lock = threading.RLock()
        self._pending_finalizations = set()
        self._pending_degradation = {}
        self.last_error = None

    def mark_degraded(self, icaos, reason):
        """Mark affected private captures without exposing an ordinary log."""
        with self._lock:
            for icao in icaos:
                capture = self._captures.get(str(icao).upper())
                if capture is not None:
                    capture.degraded = True
                    capture.degradation_reasons.add(str(reason))
                else:
                    self._pending_degradation.setdefault(
                        str(icao).upper(), set()).add(str(reason))

    def observe_record(self, record):
        """Append one already-attributed live record if its ICAO is active."""
        try:
            self.last_error = None
            with self._lock:
                capture = self._captures.get(record.icao)
                if (capture is None or capture.closed
                        or record.received_at_utc > capture.capture_until_utc):
                    return False
                return self._write_record_locked(capture, record)
        except Exception as error:
            self.last_error = str(error)
            return False

    def observe_encounter(self, state, capture_state, observer_context=None,
                          observed_at_utc=None):
        """Create/update one private manifest and shared physical capture."""
        try:
            self.last_error = None
            if not isinstance(state, CandidateEncounterState):
                return False
            observed_at = validate_utc_datetime(
                observed_at_utc if observed_at_utc is not None
                else state.last_update_utc)
            with self._lock:
                if self.full_recorder_reference is not None:
                    self._encounters[state.encounter_id] = state
                    if observer_context is not None:
                        self._encounter_contexts.setdefault(
                            state.encounter_id, observer_context)
                    marker_status = (
                        "complete" if state.completed_at_utc is not None
                        or state.encounter_id in self._completed_encounter_ids
                        else "active")
                    self._write_full_reference_manifest_locked(
                        state, marker_status)
                    return True
                capture = self._captures.get(state.icao)
                belongs_to_active_capture = (
                    capture is not None and not capture.closed
                    and state.encounter_id in capture.encounter_ids)
                storage_window_completed = (
                    state.completed_at_utc is not None
                    or state.encounter_id in self._completed_encounter_ids
                    or (state.required_end_time_utc <= observed_at
                        and not belongs_to_active_capture))
                if storage_window_completed:
                    self._encounters[state.encounter_id] = state
                    if observer_context is not None:
                        self._encounter_contexts.setdefault(
                            state.encounter_id, observer_context)
                    historical = self._encounter_captures.get(
                        state.encounter_id)
                    if historical is not None:
                        self._write_encounter_manifest_locked(
                            state, historical)
                    return True
                if capture is None or capture.closed:
                    capture = self._start_capture_locked(state, capture_state)
                if capture_state is not None:
                    capture.capture_until_utc = max(
                        capture.capture_until_utc,
                        capture_state.capture_until_utc)
                    capture.requested_capture_until_utc = max(
                        capture.requested_capture_until_utc,
                        capture_state.requested_capture_until_utc)
                    capture.hard_ceiling_applied = (
                        capture.requested_capture_until_utc
                        > capture.capture_until_utc)
                    capture.encounter_ids.update(capture_state.encounter_ids)
                capture.encounter_ids.add(state.encounter_id)
                self._encounter_captures[state.encounter_id] = capture
                self._encounters[state.encounter_id] = state
                if observer_context is not None:
                    self._encounter_contexts.setdefault(
                        state.encounter_id, observer_context)
                self._write_capture_manifest_locked(capture, "active")
                self._write_encounter_manifest_locked(state, capture)
                return True
        except Exception as error:
            self.last_error = str(error)
            return False

    def finalize_completed(self, completed_states, capture_states=None):
        """Atomically finalize manifests and close captures no longer needed."""
        try:
            self.last_error = None
            with self._lock:
                capture_states = capture_states or {}
                for icao in tuple(self._pending_finalizations):
                    if capture_states.get(icao) is None:
                        capture = self._captures.get(icao)
                        if capture is not None:
                            self._close_capture_locked(capture)
                    self._pending_finalizations.discard(icao)
                affected = set()
                for state in completed_states:
                    if not isinstance(state, CandidateEncounterState):
                        continue
                    self._encounters[state.encounter_id] = state
                    self._completed_encounter_ids.add(state.encounter_id)
                    affected.add(state.icao)
                    if self.full_recorder_reference is not None:
                        self._write_full_reference_manifest_locked(
                            state, "complete")
                        continue
                    capture = self._captures.get(state.icao)
                    if capture is not None:
                        self._write_encounter_manifest_locked(state, capture)
                for icao in affected:
                    if self.full_recorder_reference is not None:
                        continue
                    capture = self._captures.get(icao)
                    if capture is None:
                        continue
                    current = capture_states.get(icao)
                    if current is not None:
                        capture.capture_until_utc = max(
                            capture.capture_until_utc,
                            current.capture_until_utc)
                        capture.requested_capture_until_utc = max(
                            capture.requested_capture_until_utc,
                            current.requested_capture_until_utc)
                        capture.hard_ceiling_applied = (
                            capture.requested_capture_until_utc
                            > capture.capture_until_utc)
                        continue
                    try:
                        self._close_capture_locked(capture)
                    except Exception:
                        self._pending_finalizations.add(icao)
                        raise
                return True
        except Exception as error:
            self.last_error = str(error)
            return False

    def close_incomplete(self, closed_at_utc=None):
        """Close active handles while preserving inspectable incomplete data."""
        try:
            self.last_error = None
            if closed_at_utc is not None:
                validate_utc_datetime(closed_at_utc)
            with self._lock:
                if self.full_recorder_reference is not None:
                    for state in tuple(self._encounters.values()):
                        if state.unfinished:
                            self._write_full_reference_manifest_locked(
                                state, "incomplete")
                    return True
                for capture in tuple(self._captures.values()):
                    for stream_type, stream in capture.streams.items():
                        if not stream["data"].closed:
                            self._reconcile_stream_locked(
                                capture, stream_type, stream)
                    for stream in capture.streams.values():
                        if not stream["data"].closed:
                            stream["data"].flush()
                            stream["data"].close()
                        if not stream["timing"].closed:
                            stream["timing"].flush()
                            stream["timing"].close()
                    capture.streams_closed = True
                    self._write_capture_manifest_locked(capture, "incomplete")
                    for encounter_id in capture.encounter_ids:
                        state = self._encounters.get(encounter_id)
                        if state is not None:
                            self._write_encounter_manifest_locked(state, capture)
                    capture.closed = True
                self._captures.clear()
            return True
        except Exception as error:
            self.last_error = str(error)
            return False

    def _full_reference_directory_locked(self, state):
        existing = self._reference_directories.get(state.encounter_id)
        if existing is not None:
            return existing
        day = state.triggered_at_utc.strftime("%Y%m%d")
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", state.encounter_id)
        root = self.root_directory / day / state.icao / "full_references"
        collision_index = 0
        while True:
            name = (safe_id if collision_index == 0 else
                    "{}_{:02d}".format(safe_id, collision_index))
            directory = root / name
            try:
                directory.mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                collision_index += 1
        self._reference_directories[state.encounter_id] = directory
        return directory

    def _write_full_reference_manifest_locked(self, state, marker_status):
        reference = self.full_recorder_reference
        directory = self._full_reference_directory_locked(state)
        relative_session = os.path.relpath(
            Path(reference.session_directory).resolve(), directory.resolve())
        observer = self._encounter_contexts.get(state.encounter_id)
        degradation_reasons = sorted(
            self._pending_degradation.get(state.icao, ()))
        payload = {
            "schema_version": 1,
            "private_forensic_data": True,
            "storage_mode": "FULL_REFERENCE",
            "marker_status": marker_status,
            "degraded": bool(degradation_reasons),
            "degradation_reasons": degradation_reasons,
            "encounter_id": state.encounter_id,
            "observer_epoch": state.observer_epoch,
            "icao": state.icao,
            "callsign": getattr(
                state.latest_prediction, "callsign", "") or None,
            "body": state.body,
            "encounter_generation": state.encounter_generation,
            "triggered_at_utc": _utc_text(state.triggered_at_utc),
            "last_update_utc": _utc_text(state.last_update_utc),
            "completed_at_utc": _utc_text(state.completed_at_utc),
            "outcome": state.outcome.value,
            "required_window": {
                "start_utc": _utc_text(state.prebuffer_start_utc),
                "requested_end_utc": _utc_text(
                    state.requested_end_time_utc),
                "applied_end_utc": _utc_text(state.required_end_time_utc),
                "hard_end_utc": _utc_text(state.hard_end_time_utc),
                "configured_hard_ceiling_seconds": (
                    state.hard_ceiling_seconds),
                "hard_ceiling_applied": state.hard_ceiling_applied,
                "truncation_reason": (
                    "maximum_capture_duration"
                    if state.hard_ceiling_applied else None),
            },
            "prediction_geometry": getattr(
                getattr(state.latest_prediction, "model", None),
                "value", getattr(state.latest_prediction, "model", None)),
            "full_session": {
                "session_id": reference.session_id,
                "session_start_utc": _utc_text(
                    reference.session_start_utc),
                "relative_path": Path(relative_session).as_posix(),
                "streams": _json_safe(reference.streams),
            },
            "trigger_prediction": _prediction_manifest(
                state.trigger_prediction),
            "latest_prediction": _prediction_manifest(
                state.latest_prediction),
            "observer_context": _observer_manifest(observer),
        }
        _atomic_json(directory / "manifest.json", payload)

    def _start_capture_locked(self, state, capture_state):
        day = state.triggered_at_utc.strftime("%Y%m%d")
        token = re.sub(r"[^A-Za-z0-9_.-]", "_", state.encounter_id)
        base_capture_id = "{}_{}_{}".format(
            state.triggered_at_utc.strftime("%Y%m%dT%H%M%S_%fZ"),
            state.icao, token)
        capture_root = self.root_directory / day / state.icao / "captures"
        collision_index = 0
        while True:
            capture_id = (base_capture_id if collision_index == 0 else
                          "{}_{:02d}".format(base_capture_id, collision_index))
            directory = capture_root / capture_id
            try:
                directory.mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                collision_index += 1
        until = (capture_state.capture_until_utc if capture_state is not None
                 else state.required_end_time_utc)
        requested_until = (
            capture_state.requested_capture_until_utc
            if capture_state is not None else state.requested_end_time_utc)
        capture = _CandidateCapture(
            capture_id, state.icao, directory, state.prebuffer_start_utc,
            until, requested_until, state.hard_ceiling_seconds,
            bool(capture_state.hard_ceiling_applied
                 if capture_state is not None else state.hard_ceiling_applied),
            set(), {}, set(), collision_index=collision_index)
        if collision_index:
            capture.degraded = True
            capture.degradation_reasons.add("capture_directory_collision")
        pending = self._pending_degradation.pop(state.icao, set())
        if pending:
            capture.degraded = True
            capture.degradation_reasons.update(pending)
        self._captures[state.icao] = capture
        for record in self.pre_buffer.get_records_since(
                state.icao, state.prebuffer_start_utc):
            self._write_record_locked(capture, record)
        return capture

    @staticmethod
    def _record_key(record):
        sequence_id = int(getattr(record, "sequence_id", 0))
        return sequence_id if sequence_id > 0 else None

    def _write_record_locked(self, capture, record):
        key = self._record_key(record)
        if key is not None and key in capture.seen_records:
            return False
        raw = (record.raw_data.encode("utf-8")
               if isinstance(record.raw_data, str) else bytes(record.raw_data))
        stream = capture.streams.get(record.stream_type)
        if stream is None:
            filename = self.STREAM_FILES[record.stream_type]
            data_path = capture.directory / filename
            timing_path = capture.directory / (filename + ".timing.jsonl")
            stream = {
                "filename": filename, "data": data_path.open("ab+"),
                "timing": timing_path.open("a+", encoding="utf-8", newline="\n"),
                "count": 0, "bytes": 0,
                "first_received_at_utc": None, "last_received_at_utc": None,
            }
            capture.streams[record.stream_type] = stream
        offset = stream["bytes"]
        timing_offset = stream["timing"].tell()
        try:
            written = stream["data"].write(raw)
            if written != len(raw):
                raise OSError("short candidate stream write")
            stream["data"].flush()
            stream["timing"].write(json.dumps({
                "sequence_id": key,
                "received_at_utc": _utc_text(record.received_at_utc),
                "offset": offset, "length": len(raw),
            }, sort_keys=True) + "\n")
            stream["timing"].flush()
        except Exception:
            stream["data"].seek(offset)
            stream["data"].truncate()
            stream["data"].flush()
            stream["timing"].seek(timing_offset)
            stream["timing"].truncate()
            stream["timing"].flush()
            raise
        stream["count"] += 1
        stream["bytes"] += len(raw)
        timestamp = _utc_text(record.received_at_utc)
        stream["first_received_at_utc"] = (
            stream["first_received_at_utc"] or timestamp)
        stream["last_received_at_utc"] = timestamp
        if key is not None:
            capture.seen_records.add(key)
        return True

    def _stream_metadata_locked(self, capture):
        result = {}
        for stream_type, item in capture.streams.items():
            result[stream_type.value] = {
                "filename": item["filename"],
                "timing_filename": item["filename"] + ".timing.jsonl",
                "record_count": item["count"],
                "byte_count": item["bytes"],
                "first_received_at_utc": item["first_received_at_utc"],
                "last_received_at_utc": item["last_received_at_utc"],
                "recovered_unindexed_bytes": (
                    capture.recovered_unindexed_bytes.get(
                        stream_type.value, 0)),
            }
        return result

    def _write_capture_manifest_locked(self, capture, status):
        payload = {
            "schema_version": 1,
            "private_forensic_data": True,
            "capture_id": capture.capture_id,
            "icao": capture.icao,
            "status": status,
            "capture_start_utc": _utc_text(capture.started_at_utc),
            "capture_until_utc": _utc_text(capture.capture_until_utc),
            "requested_capture_until_utc": _utc_text(
                capture.requested_capture_until_utc),
            "applied_capture_until_utc": _utc_text(capture.capture_until_utc),
            "configured_hard_ceiling_seconds": capture.hard_ceiling_seconds,
            "hard_ceiling_applied": capture.hard_ceiling_applied,
            "truncation_reason": (
                "maximum_capture_duration" if capture.hard_ceiling_applied
                else None),
            "degraded": capture.degraded,
            "degradation_reasons": sorted(capture.degradation_reasons),
            "collision_index": capture.collision_index,
            "encounter_ids": sorted(capture.encounter_ids),
            "streams": self._stream_metadata_locked(capture),
        }
        _atomic_json(capture.directory / "capture_manifest.json", payload)

    def _write_encounter_manifest_locked(self, state, capture):
        day = state.triggered_at_utc.strftime("%Y%m%d")
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", state.encounter_id)
        directory = self.root_directory / day / state.icao / "encounters" / safe_id
        directory.mkdir(parents=True, exist_ok=True)
        observer = self._encounter_contexts.get(state.encounter_id)
        relative_capture = os.path.relpath(capture.directory, directory)
        payload = {
            "schema_version": 1,
            "private_forensic_data": True,
            "encounter_id": state.encounter_id,
            "observer_epoch": state.observer_epoch,
            "icao": state.icao,
            "callsign": getattr(state.latest_prediction, "callsign", "") or None,
            "body": state.body,
            "encounter_generation": state.encounter_generation,
            "triggered_at_utc": _utc_text(state.triggered_at_utc),
            "last_update_utc": _utc_text(state.last_update_utc),
            "completed_at_utc": _utc_text(state.completed_at_utc),
            "outcome": state.outcome.value,
            "required_window": {
                "start_utc": _utc_text(state.prebuffer_start_utc),
                "requested_end_utc": _utc_text(state.requested_end_time_utc),
                "applied_end_utc": _utc_text(state.required_end_time_utc),
                "hard_end_utc": _utc_text(state.hard_end_time_utc),
                "configured_hard_ceiling_seconds": (
                    state.hard_ceiling_seconds),
                "hard_ceiling_applied": state.hard_ceiling_applied,
                "truncation_reason": (
                    "maximum_capture_duration"
                    if state.hard_ceiling_applied else None),
            },
            "physical_capture": {
                "capture_id": capture.capture_id,
                "relative_path": Path(relative_capture).as_posix(),
                "streams": self._stream_metadata_locked(capture),
            },
            "trigger_prediction": _prediction_manifest(state.trigger_prediction),
            "latest_prediction": _prediction_manifest(state.latest_prediction),
            "observer_context": _observer_manifest(observer),
        }
        _atomic_json(directory / "manifest.json", payload)

    def _close_capture_locked(self, capture):
        if capture.closed:
            return
        if not capture.streams_closed:
            for stream_type, stream in capture.streams.items():
                if not stream["data"].closed:
                    self._reconcile_stream_locked(capture, stream_type, stream)
            self._write_capture_manifest_locked(capture, "finalizing")
            for encounter_id in capture.encounter_ids:
                state = self._encounters.get(encounter_id)
                if state is not None:
                    self._write_encounter_manifest_locked(state, capture)
            for stream in capture.streams.values():
                if not stream["data"].closed:
                    stream["data"].flush()
                    stream["data"].close()
                if not stream["timing"].closed:
                    stream["timing"].flush()
                    stream["timing"].close()
            capture.streams_closed = True
        self._write_capture_manifest_locked(capture, "complete")
        capture.closed = True
        self._captures.pop(capture.icao, None)

    def _reconcile_stream_locked(self, capture, stream_type, stream):
        stream["data"].flush()
        stream["timing"].flush()
        data_size = stream["data"].seek(0, os.SEEK_END)
        stream["timing"].seek(0)
        valid_lines = []
        valid_end = 0
        first = None
        last = None
        for line in stream["timing"]:
            try:
                item = json.loads(line)
                offset = int(item["offset"])
                length = int(item["length"])
                if offset != valid_end or length < 0 or offset + length > data_size:
                    break
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                break
            valid_lines.append(line)
            valid_end = offset + length
            first = first or item.get("received_at_utc")
            last = item.get("received_at_utc")
        unindexed = max(0, data_size - valid_end)
        if unindexed:
            capture.degraded = True
            capture.degradation_reasons.add("unindexed_stream_bytes_recovered")
            capture.recovered_unindexed_bytes[stream_type.value] = unindexed
            stream["data"].seek(valid_end)
            stream["data"].truncate()
            stream["data"].flush()
        stream["timing"].seek(0)
        stream["timing"].truncate()
        stream["timing"].writelines(valid_lines)
        stream["timing"].flush()
        stream["count"] = len(valid_lines)
        stream["bytes"] = valid_end
        stream["first_received_at_utc"] = first
        stream["last_received_at_utc"] = last


class CandidateStorageWorker:
    """Bounded asynchronous facade keeping candidate I/O off runtime threads."""

    DEFAULT_QUEUE_SIZE = 8192

    def __init__(self, store, max_queue_size=DEFAULT_QUEUE_SIZE,
                 thread_factory=threading.Thread):
        self.store = store
        self.max_queue_size = max(1, int(max_queue_size))
        self._queue = queue.Queue(maxsize=self.max_queue_size)
        self._stop = threading.Event()
        self._state_lock = threading.Lock()
        self._degraded_icaos = {}
        self._overflow_encounters = {}
        self._overflow_finalizes = []
        self.last_error = None
        self._thread = thread_factory(
            target=self._run, name="candidate-storage", daemon=True)
        self._thread.start()

    def enqueue_record(self, record):
        return self._enqueue("record", record, (record.icao,))

    def enqueue_encounter(self, state, capture_state, observer_context=None,
                          observed_at_utc=None):
        payload = (state, capture_state, observer_context, observed_at_utc)
        accepted = self._enqueue("encounter", payload, (state.icao,))
        if not accepted:
            with self._state_lock:
                self._overflow_encounters[state.encounter_id] = payload
        return accepted

    def enqueue_finalize(self, completed_states, capture_states):
        frozen_states = MappingProxyType(dict(capture_states))
        icaos = tuple({state.icao for state in completed_states})
        payload = (tuple(completed_states), frozen_states)
        accepted = self._enqueue("finalize", payload, icaos)
        if not accepted:
            with self._state_lock:
                self._overflow_finalizes.append(payload)
        return accepted

    def _enqueue(self, kind, payload, icaos):
        if self._stop.is_set():
            self._mark_degraded(icaos, "storage_worker_stopped")
            return False
        try:
            self._queue.put_nowait((kind, payload, tuple(icaos)))
            return True
        except queue.Full:
            self._mark_degraded(icaos, "storage_queue_overflow")
            return False

    def _mark_degraded(self, icaos, reason):
        with self._state_lock:
            for item in icaos:
                self._degraded_icaos.setdefault(
                    str(item).upper(), set()).add(str(reason))
            self.last_error = reason

    def _apply_degraded(self):
        with self._state_lock:
            degraded = self._degraded_icaos
            self._degraded_icaos = {}
        for icao, reasons in degraded.items():
            for reason in reasons:
                self.store.mark_degraded((icao,), reason)

    def _take_overflow_encounters(self):
        with self._state_lock:
            values = tuple(self._overflow_encounters.values())
            self._overflow_encounters.clear()
        return values

    def _take_overflow_finalizes(self):
        with self._state_lock:
            values = tuple(self._overflow_finalizes)
            self._overflow_finalizes.clear()
        return values

    def _process_overflow(self):
        for payload in self._take_overflow_encounters():
            self._process("encounter", payload, (payload[0].icao,))
        for payload in self._take_overflow_finalizes():
            self._process("finalize", payload,
                          tuple(state.icao for state in payload[0]))

    def _process(self, kind, payload, icaos):
        self._apply_degraded()
        if kind == "record":
            self.store.observe_record(payload)
        elif kind == "encounter":
            self.store.observe_encounter(*payload)
        elif kind == "finalize":
            self.store.finalize_completed(*payload)
        if self.store.last_error is not None:
            self._mark_degraded(icaos, "storage_worker_failure")

    def _run(self):
        while (not self._stop.is_set() or not self._queue.empty()
               or self._overflow_encounters or self._overflow_finalizes):
            try:
                kind, payload, icaos = self._queue.get(timeout=0.05)
            except queue.Empty:
                self._process_overflow()
                continue
            try:
                self._process(kind, payload, icaos)
                self._process_overflow()
            except Exception as error:
                self.last_error = str(error)
                self._mark_degraded(icaos, "storage_worker_failure")
            finally:
                self._queue.task_done()
        self._apply_degraded()
        self.store.close_incomplete(datetime.now(timezone.utc))

    def flush(self, timeout_seconds=5.0):
        deadline = time.monotonic() + float(timeout_seconds)
        while (self._queue.unfinished_tasks
               or self._overflow_encounters or self._overflow_finalizes):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True

    def close(self, timeout_seconds=5.0):
        self._stop.set()
        self._thread.join(timeout=max(0.0, float(timeout_seconds)))
        return not self._thread.is_alive()


def _utc_text(value):
    if value is None:
        return None
    return validate_utc_datetime(value).isoformat().replace("+00:00", "Z")


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return _utc_text(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {item.name: _json_safe(getattr(value, item.name))
                for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    return str(value)


def _prediction_manifest(prediction):
    if prediction is None:
        return None
    return {
        "predicted_transit_utc": _utc_text(prediction.predicted_transit_utc),
        "separation_deg": prediction.separation_deg,
        "slant_range_km": prediction.slant_range_km,
        "prediction_geometry": prediction.model,
        "boundary_status": prediction.boundary_status,
        "aircraft": {
            "latitude_deg": prediction.aircraft_latitude_deg,
            "longitude_deg": prediction.aircraft_longitude_deg,
            "altitude_m": prediction.aircraft_altitude_m,
            "azimuth_deg": prediction.aircraft_azimuth_deg,
            "altitude_angle_deg": prediction.aircraft_altitude_deg,
        },
        "body": {
            "azimuth_deg": prediction.body_azimuth_deg,
            "altitude_deg": prediction.body_altitude_deg,
            "radius_deg": prediction.body_radius_deg,
        },
        "frozen_vertical_state": _json_safe(prediction.frozen_vertical_state),
    }


def _observer_manifest(observer_context):
    if observer_context is None:
        return None
    position = observer_context.position
    return {
        "requested_mode": observer_context.requested_mode,
        "effective_source": observer_context.effective_source,
        "epoch": observer_context.epoch,
        "latitude_deg": position.latitude_deg if position is not None else None,
        "longitude_deg": position.longitude_deg if position is not None else None,
        "elevation_m": position.elevation_m if position is not None else None,
        "mobile_age_seconds": observer_context.mobile_age_seconds,
        "mobile_accuracy_m": observer_context.mobile_accuracy_m,
        "fallback_enabled": observer_context.fallback_enabled,
        "fallback_active": observer_context.fallback_active,
    }


def _atomic_json(path, payload):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
    temporary.replace(path)
