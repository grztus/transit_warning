"""Optional, fail-open Telegram notifications for predicted transits."""

from dataclasses import dataclass
from collections import Counter
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
    encounter_id: str | None = None
    prediction_geometry: str = "LEGACY"
    observer_epoch: int | None = None
    source_owner: str | None = None


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
        self._authoritative = {}
        # Latest encounter per owner, not a cross-source matching key. Remember
        # visits/closure across AUTO flips so only a newly created recipient
        # can inherit, and an old closed encounter cannot re-arm.
        self._owner_encounters = {}
        self._handoffs = {}
        self._inherited = {}
        self._notified = set()
        self._handoff_seeded = set()
        self._decisions = Counter()
        self._stability_seconds = float(stability_seconds)
        self._lock = threading.Lock()
        self._closed = False
        self._worker = threading.Thread(
            target=self._run, name="telegram-notifications", daemon=True)
        self._worker.start()

    @staticmethod
    def _event_key(event):
        pair = (event.icao.upper(), event.body.upper())
        if event.observer_epoch is not None:
            return pair + (event.observer_epoch, event.source_owner, event.encounter_id)
        return pair + (event.encounter_id,) if event.encounter_id else pair

    def record_decision(self, reason):
        with self._lock:
            self._decisions[reason] += 1

    def diagnostics(self):
        """Aggregate private counters only: no identity, position or payload."""
        with self._lock:
            return dict(self._decisions)

    def _expire_locked(self, now):
        for key in [key for key, expiry in self._events.items() if now > expiry]:
            self._events.pop(key, None)
            self._notified.discard(key)
            self._handoff_seeded.discard(key)
        for mapping in (self._handoffs, self._inherited):
            for key in [key for key, expiry in mapping.items() if now > expiry]:
                mapping.pop(key, None)

    def _seed_handoff_locked(self, identity, key):
        # One actual notification can seed only one handoff, including when
        # AUTO later revisits the original still-active LOCAL encounter ID.
        if key in self._notified and key not in self._handoff_seeded:
            self._handoffs[identity] = self._events[key]
            self._handoff_seeded.add(key)

    def observe_authoritative(self, event):
        """Bind the first replacement even when it is outside alert range.

        Only source reset/authority handoff can seed inheritance. The copied
        expiry is immutable; inherited suppression never seeds another handoff.
        These keys are notification-only and never replace lifecycle/history IDs.
        """
        identity = (event.observer_epoch, event.icao.upper(), event.body.upper())
        signature = (event.source_owner, event.encounter_id)
        with self._lock:
            self._expire_locked(event.created_at_utc)
            owner_key = identity + (event.source_owner,)
            visited = self._owner_encounters.get(owner_key)
            if visited == (event.encounter_id, True):
                return
            new_encounter = visited is None or visited[0] != event.encounter_id
            self._owner_encounters[owner_key] = (event.encounter_id, False)
            previous = self._authoritative.get(identity)
            if previous and previous[0] == signature:
                return
            if previous:
                old_signature, old_key = previous
                self._pending.pop(old_key, None)
                if old_signature[0] != event.source_owner and new_encounter:
                    self._seed_handoff_locked(identity, old_key)
            key = self._event_key(event)
            self._authoritative[identity] = (signature, key)
            expiry = self._handoffs.pop(identity, None)
            if expiry is not None and new_encounter:
                self._inherited.setdefault(key, expiry)

    def invalidate_authoritative(self, *, source_handoff, now):
        with self._lock:
            self._expire_locked(now)
            if source_handoff:
                for identity, (_, key) in self._authoritative.items():
                    self._seed_handoff_locked(identity, key)
            else:
                self._handoffs.clear()
            for _, key in self._authoritative.values():
                self._pending.pop(key, None)
            self._authoritative.clear()
            # Runtime cancellation/source generations reject old results before
            # they reach us. The new bridge has a fresh namespace and LOCAL
            # increments its lifecycle generation; retire the old owner ledger.
            self._owner_encounters.clear()

    def withdraw_authoritative(self, prediction, owner):
        identity = (prediction.observer_epoch, prediction.icao.upper(), prediction.body.upper())
        with self._lock:
            active = self._authoritative.get(identity)
            changed = False
            # A late withdrawal from the old AUTO owner must not cancel its successor.
            if active and active[0] == (owner, prediction.encounter_id):
                self._pending.pop(active[1], None)
                self._authoritative.pop(identity, None)
                changed = True
            owner_key = identity + (owner,)
            visited = self._owner_encounters.get(owner_key)
            if visited is not None and visited[0] == prediction.encounter_id:
                self._owner_encounters[owner_key] = (prediction.encounter_id, True)
                changed = changed or not visited[1]
            if changed:
                self._decisions['SKIPPED_LIFECYCLE'] += 1

    def finalize_authoritative(self, icao, body, encounter_id, reason):
        """Consume actual dashboard finalization, independently of its journal."""
        if encounter_id is None:
            return  # LEGACY notification semantics are unchanged.
        with self._lock:
            for identity, (signature, key) in list(self._authoritative.items()):
                if identity[1:] != (icao.upper(), body.upper()) or signature[1] != encounter_id:
                    continue
                self._owner_encounters[identity + (signature[0],)] = (encounter_id, True)
                self._pending.pop(key, None)
                # Replacement closes the old ID, but the incoming owner's
                # transition still needs it to decide whether this is a source
                # handoff. Ordinary PASSED/WITHDRAWN cannot seed inheritance.
                if reason != "PREDICTION_REPLACED":
                    self._authoritative.pop(identity, None)
                self._decisions['SKIPPED_LIFECYCLE'] += 1

    def _closed_authoritative_locked(self, event):
        owner_key = (event.observer_epoch, event.icao.upper(), event.body.upper(), event.source_owner)
        if self._owner_encounters.get(owner_key) == (event.encounter_id, True):
            self._pending.pop(self._event_key(event), None)
            self._decisions['SKIPPED_LIFECYCLE'] += 1
            return True
        return False

    def _handoff_blocked_locked(self, key, now):
        expiry = self._inherited.get(key)
        if expiry is not None and now <= expiry:
            self._pending.pop(key, None)
            self._decisions['SKIPPED_SOURCE_HANDOFF'] += 1
            return True
        return False

    def notify(self, event):
        now = event.created_at_utc
        with self._lock:
            key = self._event_key(event)
            if self._closed:
                return False
            self._expire_locked(now)
            if self._closed_authoritative_locked(event):
                return False
            if self._handoff_blocked_locked(key, now):
                return False
            self._pending.pop(key, None)
            if key in self._events:
                self._decisions['SKIPPED_DUPLICATE'] += 1
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
                self._notified.add(key)
                self._decisions['ENQUEUED'] += 1
                return True
            except queue.Full:
                self._decisions['QUEUE_FULL'] += 1
        self._report("Telegram notification queue is full")
        return False

    def consider(self, event):
        """Accept an eligible event only after continuous stable eligibility."""
        if self._stability_seconds <= 0:
            return self.notify(event)
        now = event.created_at_utc
        with self._lock:
            key = self._event_key(event)
            if self._closed:
                return False
            self._expire_locked(now)
            if self._closed_authoritative_locked(event):
                return False
            if self._handoff_blocked_locked(key, now):
                return False
            if key in self._events:
                self._decisions['SKIPPED_DUPLICATE'] += 1
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
                self._decisions['PENDING_STABILITY'] += 1
                return False
            started_at, _ = pending
            self._pending[key] = (started_at, event)
            ready = (now - started_at).total_seconds() >= self._stability_seconds
            if ready:
                self._pending.pop(key, None)
            else:
                self._decisions['PENDING_STABILITY'] += 1
        return self.notify(event) if ready else False

    def cancel(self, icao, body):
        with self._lock:
            prefix = (icao.upper(), body.upper())
            keys = [key for key in self._pending if key[:2] == prefix]
            for key in keys:
                self._pending.pop(key, None)
            return bool(keys)

    def cancel_aircraft(self, icao):
        changed = False
        for body in ("SUN", "MOON"):
            changed = self.cancel(icao, body) or changed
        return changed

    def suppress(self, event):
        """Mark an eligible event seen without queueing a network message."""
        expiry = event.predicted_transit_utc + datetime.timedelta(
            seconds=EVENT_EXPIRY_GRACE_SECONDS)
        with self._lock:
            key = self._event_key(event)
            if self._closed:
                return False
            self._expire_locked(event.created_at_utc)
            if self._closed_authoritative_locked(event):
                return False
            if self._handoff_blocked_locked(key, event.created_at_utc):
                return False
            self._pending.pop(key, None)
            self._events[key] = max(self._events.get(key, expiry), expiry)
        return True

    def suppress_body(self, body):
        """Convert pending events for one body into deduplicated seen events."""
        body = body.upper()
        with self._lock:
            pending = [(key, value) for key, value in self._pending.items()
                       if key[1] == body]
            for key, (_, event) in pending:
                expiry = event.predicted_transit_utc + datetime.timedelta(
                    seconds=EVENT_EXPIRY_GRACE_SECONDS)
                self._events[key] = max(
                    self._events.get(key, expiry), expiry)
                self._pending.pop(key, None)
        return bool(pending)

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
    def finalize_authoritative(self, icao, body, encounter_id, reason):
        pass

    def observe_authoritative(self, event):
        pass

    def invalidate_authoritative(self, **kwargs):
        pass

    def withdraw_authoritative(self, prediction, owner):
        pass

    def record_decision(self, reason):
        pass

    def diagnostics(self):
        return {}

    def notify(self, event):
        return False

    def consider(self, event):
        return False

    def cancel(self, icao, body):
        return False

    def cancel_aircraft(self, icao):
        return False

    def suppress(self, event):
        return False

    def suppress_body(self, body):
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
