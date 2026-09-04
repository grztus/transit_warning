"""Bounded latest-state delivery for public Server-Sent Events."""
from copy import deepcopy
import json
import threading

from .contracts import SCHEMA_VERSION
from .privacy import assert_public_payload


def live_envelope(snapshot):
    state = deepcopy(snapshot["state"])
    value = {"schema_version": SCHEMA_VERSION, "event": "live_state",
             "live_revision": snapshot["revision"],
             "generated_at_utc": state.get("generated_at_utc"),
             "payload": state}
    assert_public_payload(value)
    return value


def settings_envelope(snapshot):
    value = {"schema_version": SCHEMA_VERSION, "event": "settings",
             "settings_revision": snapshot["revision"],
             "payload": deepcopy(snapshot)}
    assert_public_payload(value)
    return value


def encode_sse(value):
    return "event: {}\ndata: {}\n\n".format(
        value["event"], json.dumps(value, separators=(",", ":")))


class Subscription:
    def __init__(self, broker):
        self.broker = broker
        self.condition = threading.Condition()
        self.latest = {}
        self.closed = False

    def offer(self, value):
        with self.condition:
            if not self.closed:
                self.latest[value["event"]] = value
                self.condition.notify()

    def next(self, timeout):
        with self.condition:
            if not self.latest and not self.closed:
                self.condition.wait(timeout)
            values = tuple(self.latest.values())
            self.latest.clear()
            return values

    def close(self):
        with self.condition:
            self.closed = True
            self.latest.clear()
            self.condition.notify_all()
        self.broker.remove(self)


class SseBroker:
    def __init__(self):
        self.clients = set()
        self.lock = threading.Lock()
        self.last_revisions = {}

    def subscribe(self, initial=()):
        client = Subscription(self)
        with self.lock:
            self.clients.add(client)
        for value in initial:
            client.offer(value)
        return client

    def remove(self, client):
        with self.lock:
            self.clients.discard(client)

    def publish(self, value):
        with self.lock:
            revision_key = ("live_revision" if value["event"] == "live_state"
                            else "settings_revision")
            revision = value.get(revision_key)
            previous = self.last_revisions.get(value["event"])
            if (isinstance(revision, int) and isinstance(previous, int)
                    and revision <= previous):
                return
            if isinstance(revision, int):
                self.last_revisions[value["event"]] = revision
            clients = tuple(self.clients)
        for client in clients:
            client.offer(deepcopy(value))

    @property
    def client_count(self):
        with self.lock:
            return len(self.clients)
