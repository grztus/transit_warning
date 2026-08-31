"""Optional, fail-open Telegram notifications for predicted transits."""

from dataclasses import dataclass
import datetime
import json
import queue
import threading
import urllib.error
import urllib.parse
import urllib.request


UTC = datetime.timezone.utc
TELEGRAM_API_BASE = "https://api.telegram.org"
DEFAULT_TIMEOUT_SECONDS = 5.0
EVENT_EXPIRY_GRACE_SECONDS = 60.0


@dataclass(frozen=True)
class TransitNotification:
    created_at_utc: datetime.datetime
    body: str
    icao: str
    callsign: str | None
    predicted_transit_utc: datetime.datetime
    time_to_event_seconds: float
    separation_deg: float
    body_azimuth_deg: float
    body_altitude_deg: float
    aircraft_altitude_deg: float
    distance_km: float | None


class TelegramTransport:
    def __init__(self, token, chat_id, timeout=DEFAULT_TIMEOUT_SECONDS,
                 opener=None):
        self._token = token
        self._chat_id = chat_id
        self._timeout = timeout
        self._opener = opener or urllib.request.urlopen

    def send(self, text):
        url = "{}/bot{}/sendMessage".format(TELEGRAM_API_BASE, self._token)
        request = urllib.request.Request(
            url,
            data=urllib.parse.urlencode({
                "chat_id": self._chat_id,
                "text": text,
            }).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                payload = response.read()
                if getattr(response, "status", 200) != 200:
                    return False, "HTTP {}".format(response.status)
            result = json.loads(payload.decode("utf-8"))
            if result.get("ok") is True:
                return True, None
            return False, "Telegram API rejected the message"
        except (OSError, ValueError, urllib.error.URLError) as error:
            return False, "{}".format(type(error).__name__)


def format_transit_notification(event):
    icon = "☀️" if event.body.upper() == "SUN" else "🌙"
    identity = event.callsign.strip() if event.callsign else event.icao
    distance = (
        "\ndistance: {:.0f} km".format(event.distance_km)
        if event.distance_km is not None else "")
    return (
        "{} {} — potential transit\n"
        "{}\n"
        "in {:.0f} s\n\n"
        "SEP: {:.2f}°\n"
        "ALT: {:.1f}°\n"
        "AZ: {:.0f}°{}"
    ).format(
        icon, event.body.upper(), identity, event.time_to_event_seconds,
        event.separation_deg, event.body_altitude_deg,
        event.body_azimuth_deg, distance)


def format_test_notification(now_utc):
    return (
        "✈️ Transit Warning TEST\n\n"
        "Telegram notifications are working.\n"
        "Observer: HOME\n"
        "Time: {} UTC"
    ).format(now_utc.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"))


class TelegramNotifier:
    """Queue notifications without blocking the prediction input path."""

    def __init__(self, transport, error_handler=None, stability_seconds=5.0):
        self._transport = transport
        self._error_handler = error_handler or (lambda message: None)
        self._queue = queue.Queue(maxsize=100)
        self._events = {}
        self._pending = {}
        self._stability_seconds = float(stability_seconds)
        self._lock = threading.Lock()
        self._closed = False
        self._worker = threading.Thread(
            target=self._run, name="telegram-notifications", daemon=True)
        self._worker.start()

    def notify(self, event):
        key = (event.icao.upper(), event.body.upper())
        now = event.created_at_utc
        with self._lock:
            if self._closed:
                return False
            expired = [item for item, expiry in self._events.items()
                       if now > expiry]
            for item in expired:
                self._events.pop(item, None)
            self._pending.pop(key, None)
            if key in self._events:
                current = self._events[key]
                proposed = event.predicted_transit_utc + datetime.timedelta(
                    seconds=EVENT_EXPIRY_GRACE_SECONDS)
                if proposed > current:
                    self._events[key] = proposed
                return False
            self._events[key] = event.predicted_transit_utc + datetime.timedelta(
                seconds=EVENT_EXPIRY_GRACE_SECONDS)
        try:
            self._queue.put_nowait(format_transit_notification(event))
            return True
        except queue.Full:
            self._report("Telegram notification queue is full")
            return False

    def consider(self, event):
        """Accept an eligible event only after continuous stable eligibility."""
        if self._stability_seconds <= 0:
            return self.notify(event)
        key = (event.icao.upper(), event.body.upper())
        now = event.created_at_utc
        with self._lock:
            if self._closed:
                return False
            expired = [item for item, expiry in self._events.items()
                       if now > expiry]
            for item in expired:
                self._events.pop(item, None)
            if key in self._events:
                current = self._events[key]
                proposed = event.predicted_transit_utc + datetime.timedelta(
                    seconds=EVENT_EXPIRY_GRACE_SECONDS)
                if proposed > current:
                    self._events[key] = proposed
                self._pending.pop(key, None)
                return False
            pending = self._pending.get(key)
            if pending is None:
                self._pending[key] = (now, event)
                return False
            started_at, _ = pending
            self._pending[key] = (started_at, event)
            ready = (now - started_at).total_seconds() >= self._stability_seconds
            if ready:
                self._pending.pop(key, None)
        return self.notify(event) if ready else False

    def cancel(self, icao, body):
        with self._lock:
            return self._pending.pop(
                (icao.upper(), body.upper()), None) is not None

    def cancel_aircraft(self, icao):
        changed = False
        for body in ("SUN", "MOON"):
            changed = self.cancel(icao, body) or changed
        return changed

    def send_test(self, now_utc):
        success, error = self._transport.send(format_test_notification(now_utc))
        if not success:
            self._report("Telegram test notification failed: {}".format(error))
        return success

    def _report(self, message):
        try:
            self._error_handler(message)
        except Exception:
            pass

    def _run(self):
        while True:
            try:
                message = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if message is None:
                break
            success, error = self._transport.send(message)
            if not success:
                self._report("Telegram notification failed: {}".format(error))

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._queue.put(None, timeout=0.2)
        except queue.Full:
            pass
        self._worker.join(timeout=1.0)


class DisabledTelegramNotifier:
    def notify(self, event):
        return False

    def consider(self, event):
        return False

    def cancel(self, icao, body):
        return False

    def cancel_aircraft(self, icao):
        return False

    def close(self):
        return None


def create_telegram_notifier(enabled, token, chat_id, error_handler=None,
                             transport_factory=TelegramTransport,
                             stability_seconds=5.0):
    if not enabled:
        return DisabledTelegramNotifier()
    if not token or not chat_id:
        raise ValueError("Telegram notifications require bot token and chat ID")
    return TelegramNotifier(
        transport_factory(token, chat_id), error_handler=error_handler,
        stability_seconds=stability_seconds)
