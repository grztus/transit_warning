"""Immutable observer position contexts used by production geometry."""

from dataclasses import dataclass
import datetime
from enum import Enum
import threading


UTC = datetime.timezone.utc


@dataclass(frozen=True)
class ObserverPosition:
    """One complete static observer position in decimal degrees and metres."""

    latitude_deg: float
    longitude_deg: float
    elevation_m: float

    @property
    def coordinates(self):
        return (self.latitude_deg, self.longitude_deg)


class StaticObserverPositionProvider:
    """Return the single immutable observer configured at application start."""

    def __init__(self, position):
        if not isinstance(position, ObserverPosition):
            raise TypeError("position must be an ObserverPosition")
        self._position = position

    def current(self):
        return self._position


class ObserverMode(str, Enum):
    STATIC = "STATIC"
    MOBILE = "MOBILE"
    MANUAL = "MANUAL"


class ObserverSource(str, Enum):
    STATIC = "STATIC"
    MANUAL = "MANUAL"
    MOBILE_FRESH = "MOBILE_FRESH"
    MOBILE_LAST_KNOWN = "MOBILE_LAST_KNOWN"
    STATIC_FALLBACK = "STATIC_FALLBACK"
    MOBILE_NO_FIX = "MOBILE_NO_FIX"


@dataclass(frozen=True)
class ObserverContext:
    """One frozen observer decision for a complete prediction operation."""

    position: ObserverPosition | None
    requested_mode: str
    effective_source: str
    mobile_age_seconds: float | None = None
    mobile_accuracy_m: float | None = None
    fallback_enabled: bool = False
    fallback_active: bool = False
    epoch: int = 0


class RuntimeObserverPositionProvider:
    """Resolve STATIC/MOBILE/MANUAL state without persisting phone location."""

    def __init__(self, static_position, mode="STATIC", fresh_seconds=15.0,
                 fallback_enabled=False, change_handler=None,
                 manual_position=None):
        if not isinstance(static_position, ObserverPosition):
            raise TypeError("static_position must be an ObserverPosition")
        self._static_position = static_position
        self._manual_position = manual_position or static_position
        if not isinstance(self._manual_position, ObserverPosition):
            raise TypeError("manual_position must be an ObserverPosition")
        self._mode = ObserverMode(str(mode).upper())
        self._fresh_seconds = float(fresh_seconds)
        self._fallback_enabled = bool(fallback_enabled)
        self._mobile_state = None
        self._change_handler = change_handler
        self._effective_source = None
        self._epoch = 0
        self._lock = threading.RLock()

    def attach_mobile_state(self, mobile_state):
        with self._lock:
            self._mobile_state = mobile_state

    @property
    def static_position(self):
        return self._static_position

    @property
    def manual_position(self):
        return self._manual_position

    def set_change_handler(self, handler):
        with self._lock:
            self._change_handler = handler

    def current(self, now_utc=None):
        return self.resolve(now_utc).position

    def resolve(self, now_utc=None):
        now_utc = (now_utc or datetime.datetime.now(UTC)).astimezone(UTC)
        handler = None
        with self._lock:
            position = (self._mobile_state.latest_position()
                        if self._mobile_state is not None else None)
            age = None
            if position is not None:
                age = max(0.0, (now_utc - position.received_at_utc).total_seconds())
            if self._mode is ObserverMode.STATIC:
                resolved = self._static_position
                source = ObserverSource.STATIC
            elif self._mode is ObserverMode.MANUAL:
                resolved = self._manual_position
                source = ObserverSource.MANUAL
            elif position is None:
                if self._fallback_enabled:
                    resolved = self._static_position
                    source = ObserverSource.STATIC_FALLBACK
                else:
                    resolved = None
                    source = ObserverSource.MOBILE_NO_FIX
            elif age <= self._fresh_seconds:
                resolved = ObserverPosition(
                    position.latitude, position.longitude,
                    self._static_position.elevation_m)
                source = ObserverSource.MOBILE_FRESH
            elif self._fallback_enabled:
                resolved = self._static_position
                source = ObserverSource.STATIC_FALLBACK
            else:
                resolved = ObserverPosition(
                    position.latitude, position.longitude,
                    self._static_position.elevation_m)
                source = ObserverSource.MOBILE_LAST_KNOWN
            if self._effective_source is not None and source != self._effective_source:
                self._epoch += 1
                handler = self._change_handler
            self._effective_source = source
            context = ObserverContext(
                position=resolved,
                requested_mode=self._mode.value,
                effective_source=source.value,
                mobile_age_seconds=age,
                mobile_accuracy_m=(position.accuracy_m if position else None),
                fallback_enabled=self._fallback_enabled,
                fallback_active=source is ObserverSource.STATIC_FALLBACK,
                epoch=self._epoch,
            )
        if handler is not None:
            handler(context)
        return context

    def set_manual_position(self, position, now_utc=None):
        if not isinstance(position, ObserverPosition):
            raise TypeError("position must be an ObserverPosition")
        with self._lock:
            changed = position != self._manual_position
            self._manual_position = position
            if changed and self._mode is ObserverMode.MANUAL:
                self._effective_source = None
                self._epoch += 1
                handler = self._change_handler
            else:
                handler = None
        context = self.resolve(now_utc)
        if handler is not None:
            handler(context)
        return context

    def set_mode(self, mode, now_utc=None):
        mode = ObserverMode(str(mode).upper())
        with self._lock:
            changed = mode != self._mode
            self._mode = mode
            if changed:
                self._effective_source = None
                self._epoch += 1
                handler = self._change_handler
            else:
                handler = None
        context = self.resolve(now_utc)
        if handler is not None:
            handler(context)
        return context

    def set_fallback_enabled(self, enabled, now_utc=None):
        with self._lock:
            changed = bool(enabled) != self._fallback_enabled
            self._fallback_enabled = bool(enabled)
            if changed:
                self._effective_source = None
                self._epoch += 1
                handler = self._change_handler
            else:
                handler = None
        context = self.resolve(now_utc)
        if handler is not None:
            handler(context)
        return context
