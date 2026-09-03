"""Candidate Auto-Recorder Phase 1: In-memory stream demultiplexing and pre-buffering.

This module provides the bounded in-memory pre-buffering foundation for the
Candidate Auto-Recorder. Buffering is keyed strictly by ICAO identity (not
encounter identity). It performs early filtering and demultiplexing of incoming
streams so that only reliably attributed data for known ICAOs is buffered.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
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
class CandidateEncounterState:
    """One triggered authoritative encounter and its immutable window state."""

    encounter_id: str
    observer_epoch: int
    icao: str
    body: str
    encounter_generation: int
    triggered_at_utc: datetime
    prebuffer_start_utc: datetime
    required_end_time_utc: datetime
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
    capture_until_utc: datetime
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

    def __init__(self, pre_buffer=None,
                 trigger_horizon_seconds=DEFAULT_TRIGGER_HORIZON_SECONDS,
                 trigger_separation_deg=DEFAULT_TRIGGER_SEPARATION_DEG,
                 post_t0_seconds=DEFAULT_POST_T0_SECONDS):
        self.pre_buffer = pre_buffer
        self.trigger_horizon_seconds = float(trigger_horizon_seconds)
        self.trigger_separation_deg = float(trigger_separation_deg)
        self.post_t0_seconds = float(post_t0_seconds)
        if self.trigger_horizon_seconds <= 0:
            raise ValueError("trigger horizon must be positive")
        if self.trigger_separation_deg < 0:
            raise ValueError("trigger separation must be non-negative")
        if self.post_t0_seconds < 0:
            raise ValueError("post-T0 duration must be non-negative")
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
                    required_end = prediction.predicted_transit_utc + timedelta(
                        seconds=self.post_t0_seconds)
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
                        required_end_time_utc=required_end,
                        trigger_prediction=prediction,
                        latest_prediction=prediction,
                        outcome=CandidateEncounterOutcome.ACTIVE,
                        last_update_utc=now)
                elif existing.unfinished:
                    proposed_end = prediction.predicted_transit_utc + timedelta(
                        seconds=self.post_t0_seconds)
                    existing = self._replace_encounter(
                        existing,
                        required_end_time_utc=max(
                            existing.required_end_time_utc, proposed_end),
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
        self._captures[icao] = CandidateIcaoCaptureState(
            icao=icao,
            prebuffer_start_utc=min(
                state.prebuffer_start_utc for state in unfinished),
            capture_until_utc=max(
                state.required_end_time_utc for state in unfinished),
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
            self._auto_prune_if_due_locked(current_time)
            buf = self._get_or_create_buffer(icao, current_time)
            buf.append(record)
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
            self._auto_prune_if_due_locked(current_time)
            buf = self._get_or_create_buffer(icao, current_time)
            buf.append(record)
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
            self._auto_prune_if_due_locked(current_time)
            buf = self._get_or_create_buffer(icao, current_time)
            buf.append(record)
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
            self._auto_prune_if_due_locked(current_time)
            buf = self._get_or_create_buffer(icao, current_time)
            buf.append(record)
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
                    self._auto_prune_if_due_locked(current_time)
                    buf = self._get_or_create_buffer(icao, current_time)
                    buf.append(record)
                    attributed_icaos.append(icao)
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
