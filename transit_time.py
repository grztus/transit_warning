import datetime
import statistics
from zoneinfo import ZoneInfo


class PortTimestampError(ValueError):
    """Raised when a source timestamp cannot be mapped to one UTC instant."""


class AmbiguousPortTimestampError(PortTimestampError):
    """Raised for a local timestamp that occurs twice during a DST fold."""


class NonexistentPortTimestampError(PortTimestampError):
    """Raised for a local timestamp skipped by a forward clock transition."""


def _local_timestamp_to_utc(timestamp, timezone_name):
    if timestamp.tzinfo is not None:
        raise PortTimestampError("ADS-B port 30003 timestamp must be naive local time")
    source_timezone = ZoneInfo(timezone_name)
    candidates = []
    for fold in (0, 1):
        local = timestamp.replace(tzinfo=source_timezone, fold=fold)
        timestamp_utc = local.astimezone(datetime.timezone.utc)
        roundtrip = timestamp_utc.astimezone(source_timezone)
        if roundtrip.replace(tzinfo=None) == timestamp:
            candidates.append(timestamp_utc)
    unique_candidates = set(candidates)
    if not unique_candidates:
        raise NonexistentPortTimestampError(
            "nonexistent local ADS-B timestamp {} in {}".format(timestamp, timezone_name))
    if len(unique_candidates) > 1:
        raise AmbiguousPortTimestampError(
            "ambiguous local ADS-B timestamp {} in {}".format(timestamp, timezone_name))
    return unique_candidates.pop()


def port_timestamp_to_utc(
    timestamp, port, adsb_timestamp_timezone=None, adsb_port=30003
):
    """Convert a source timestamp to timezone-aware UTC."""
    if port == adsb_port:
        if not adsb_timestamp_timezone:
            raise PortTimestampError("ADS-B timestamp timezone is required for port 30003")
        return _local_timestamp_to_utc(timestamp, adsb_timestamp_timezone)
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=datetime.timezone.utc)
    return timestamp.astimezone(datetime.timezone.utc)


def _format_utc_offset(seconds):
    sign = "+" if seconds >= 0 else "-"
    seconds = abs(int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    return "UTC{}{:02d}:{:02d}".format(sign, hours, remainder // 60)


class AdsBTimestampOffsetValidator:
    """One-shot, fail-open comparison of live SBS wall time with configured UTC."""

    def __init__(self, timezone_name, reporter=print, sample_count=3, tolerance_seconds=1800):
        self.timezone_name = timezone_name
        self.reporter = reporter
        self.sample_count = sample_count
        self.tolerance_seconds = tolerance_seconds
        self._differences = []
        self.complete = False

    def observe(self, raw_timestamp, now_utc):
        if self.complete:
            return
        try:
            expected_utc = _local_timestamp_to_utc(raw_timestamp, self.timezone_name)
            now_utc = now_utc.astimezone(datetime.timezone.utc)
            expected_offset = (raw_timestamp - expected_utc.replace(tzinfo=None)).total_seconds()
            observed_offset = (raw_timestamp - now_utc.replace(tzinfo=None)).total_seconds()
            self._differences.append((expected_offset, observed_offset))
            if len(self._differences) < self.sample_count:
                return
            expected = statistics.median(item[0] for item in self._differences)
            observed = statistics.median(item[1] for item in self._differences)
            status = "OK" if abs(observed - expected) < self.tolerance_seconds else "WARNING"
            self.reporter(
                "Configured ADS-B timezone: {}\nExpected offset:           {}\n"
                "Observed SBS offset:       approximately {}\nTimestamp check:           {}".format(
                    self.timezone_name, _format_utc_offset(expected),
                    _format_utc_offset(observed), status))
            self.complete = True
        except Exception as error:
            self.complete = True
            try:
                self.reporter("ADS-B timestamp check unavailable: {}".format(error))
            except Exception:
                pass
