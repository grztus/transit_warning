"""Immutable observer position context used by production geometry."""

from dataclasses import dataclass


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
