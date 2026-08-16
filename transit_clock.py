import argparse
import datetime
import threading

import ephem
import pytz


class Clock(object):
    def is_ready(self):
        raise NotImplementedError

    def now_utc(self):
        raise NotImplementedError

    def ephem_now(self):
        raise NotImplementedError


class RealClock(Clock):
    def is_ready(self):
        return True

    def now_utc(self):
        return datetime.datetime.now(pytz.utc)

    def ephem_now(self):
        return ephem.now()


class ReplayClock(Clock):
    def __init__(self):
        self._lock = threading.Lock()
        self._current_utc = None

    def is_ready(self):
        with self._lock:
            return self._current_utc is not None

    def advance_to(self, timestamp_utc):
        if timestamp_utc.tzinfo is None or timestamp_utc.utcoffset() != datetime.timedelta(0):
            raise ValueError("ReplayClock requires a timezone-aware UTC timestamp")
        timestamp_utc = timestamp_utc.astimezone(pytz.utc)
        with self._lock:
            if self._current_utc is None or timestamp_utc > self._current_utc:
                self._current_utc = timestamp_utc

    def _snapshot(self):
        with self._lock:
            if self._current_utc is None:
                raise RuntimeError("ReplayClock is not ready")
            return self._current_utc

    def now_utc(self):
        return self._snapshot()

    def ephem_now(self):
        return ephem.Date(self._snapshot())


def clock_from_args(arguments):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--clock", choices=("real", "replay"), default="real")
    args, remaining = parser.parse_known_args(arguments)
    if remaining:
        parser.error("unrecognized arguments: {}".format(" ".join(remaining)))
    return ReplayClock() if args.clock == "replay" else RealClock()
