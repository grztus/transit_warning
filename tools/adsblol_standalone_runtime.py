"""Experimental standalone input adapter; no production runtime/feed imports."""
import datetime as dt
import math
import threading
import time
from types import SimpleNamespace

import ephem

from auto_fusion import (
    fuse_aircraft, predictor_view, serialize_predictor_provenance,
)
from authoritative_transit import AuthoritativeTransitLifecycle, AuthoritativeTransitionKind
from live_dashboard import DashboardCandidate
from shadow_2d_prediction import Shadow2DConfig, ShadowEncounterContext, run_shadow_pipeline, message_aircraft_cache
from transit_prediction_model import (
    QNH_CORRECTION_FT_PER_HPA, current_vertical_prediction_policy,
    horizontal_position_from_t0, precise_angular_position_from_observer,
)
from tools.adsblol_live import AdsbLolProvider, MIN_POLL_SECONDS, UTC, finite


SOURCE = "ADSBLOL_STANDALONE"
TRUST = "DEGRADED_UNKNOWN_FIELD_AGES_FROZEN_ALTITUDE"


def scope(context):
    # No MOBILE position is read, compared, logged or serialized.
    if context.requested_mode not in ("STATIC", "MANUAL"):
        return (context.requested_mode, context.epoch)
    return (context.requested_mode, context.epoch, context.position)


class SnapshotPoller:
    """One HTTP worker and one replaceable mailbox; never a prediction queue."""
    def __init__(self, observer, *, provider=None, radius_nm=100, poll_seconds=10,
                 now=None, monotonic=None):
        if not finite(poll_seconds) or poll_seconds < MIN_POLL_SECONDS:
            raise ValueError("Diagnostic polling must be at least 10 seconds")
        if not finite(radius_nm) or radius_nm < 0 or int(radius_nm) != radius_nm:
            raise ValueError("Radius must be a non-negative integer NM value")
        self.observer, self.radius = observer, min(int(radius_nm), 250)
        self.poll_seconds = poll_seconds
        self.now = now or (lambda: dt.datetime.now(UTC))
        self.monotonic = monotonic or time.monotonic
        self.stop = threading.Event()
        self.provider = provider or AdsbLolProvider(max_retries=0, cancel_event=self.stop)
        self.lock = threading.Lock()
        self.in_flight = threading.Lock()
        self.sequence = 0
        self.latest = None
        self.thread = None

    def fetch_once(self):
        if not self.in_flight.acquire(blocking=False):
            return None
        try:
            return self._fetch_once()
        finally:
            self.in_flight.release()

    def _fetch_once(self):
        context = self.observer.resolve(self.now())
        identity = scope(context)
        if context.requested_mode not in ("STATIC", "MANUAL"):
            report = {"status": "PRIVACY_BLOCKED" if context.requested_mode == "MOBILE" else "MANUAL_REQUIRED",
                      "aircraft": [], "retry_after_seconds": None}
        else:
            position = context.position
            try:
                report = self.provider.acquire(observer_mode=context.requested_mode, lat=position.latitude_deg,
                                               lon=position.longitude_deg, radius_nm=self.radius)
            except Exception:
                # No exception text: HTTP libraries may include the query centre.
                report = {"status": "PROVIDER_ERROR", "aircraft": [], "retry_after_seconds": None}
        if scope(self.observer.resolve(self.now())) != identity or self.stop.is_set():
            return None
        with self.lock:
            self.sequence += 1
            self.latest = (self.sequence, identity, report, self.monotonic())
        return report

    def snapshot(self):
        with self.lock:
            return self.latest

    def _run(self):
        while not self.stop.is_set():
            report = self.fetch_once()
            delay = max(self.poll_seconds, (report or {}).get("retry_after_seconds") or 0)
            if self.stop.wait(delay):
                return

    def start(self):
        if self.thread is not None:
            raise RuntimeError("Poller already started")
        self.thread = threading.Thread(target=self._run, name="adsblol-acquisition", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=1)


class StandaloneBridge:
    """Canonical solver/lifecycle consumers with explicitly degraded inputs.

    No MotionParameter timestamps are synthesized. Motion/intent remain None,
    selecting the shared model's existing frozen-altitude behavior.
    """
    def __init__(self, poller, dashboard, geoid, *, max_position_age=20,
                 qnh_hpa=1013.25, solver=run_shadow_pipeline, now=None, monotonic=None,
                 provider_stale_seconds=30, source=SOURCE,
                 source_mode=SOURCE):
        if not finite(max_position_age) or max_position_age <= 0:
            raise ValueError("Position age limit must be finite and positive")
        if not finite(qnh_hpa) or not 800 <= qnh_hpa <= 1100:
            raise ValueError("Explicit diagnostic QNH must be between 800 and 1100 hPa")
        if geoid is None:
            raise ValueError("TRUE_2D standalone requires EGM96 geoid data")
        if not finite(provider_stale_seconds) or provider_stale_seconds <= 0:
            raise ValueError("Provider stale interval must be finite and positive")
        self.provider_stale_seconds = provider_stale_seconds
        self.last_success_receipt = None
        self.refresh_failed = False
        self.poller, self.dashboard, self.geoid = poller, dashboard, geoid
        self.max_age, self.qnh, self.solver = max_position_age, qnh_hpa, solver
        self.now = now or poller.now
        self.monotonic = monotonic or poller.monotonic
        self.lifecycle = AuthoritativeTransitLifecycle("TRUE_2D")
        self.config = Shadow2DConfig(enabled=True)
        self.identity = None
        self.sequence = 0
        self.aircraft = {}
        self.anchors = {}
        self.position_evidence = {}
        self.processed = set()
        self.missing = set()
        self.status = "WAITING"
        self.failures = 0
        self.source = source
        self.source_mode = source_mode

    def body_position(self, name, when, observer):
        obj = ephem.Observer()
        obj.lat, obj.lon = str(observer.latitude_deg), str(observer.longitude_deg)
        obj.elevation, obj.pressure = observer.elevation_m, 0
        obj.date = ephem.Date(when.astimezone(UTC))
        body = {"sun": ephem.Sun, "moon": ephem.Moon}[name.lower()](obj)
        body.compute(obj)
        return SimpleNamespace(azimuth_deg=math.degrees(body.az), altitude_deg=math.degrees(body.alt),
                               angular_diameter_arcsec=float(body.size), evaluated_at_utc=when)

    def aircraft_los(self, observer, position, amsl_m):
        return precise_angular_position_from_observer(
            observer.coordinates, observer.elevation_m + self.geoid.undulation_m(*observer.coordinates),
            position, amsl_m + self.geoid.undulation_m(*position))

    def _clear(self, now):
        self.last_success_receipt = None
        self.refresh_failed = False
        self.lifecycle.invalidate_transitions()
        self.dashboard.invalidate_source(self, now)
        if self.source_mode != "AUTO":
            self.dashboard.clear_body_positions()
        self.aircraft.clear()
        self.anchors.clear()
        self.position_evidence.clear()
        self.processed.clear()
        self.missing.clear()

    def _remove(self, icao, now, reason="WITHDRAWN"):
        self.lifecycle.discard_aircraft_transitions(icao)
        for body in ("SUN", "MOON"):
            self.dashboard.withdraw_source(icao, body, self, now,
                                           reason=reason)
        self.aircraft.pop(icao, None)
        self.anchors.pop(icao, None)
        self.processed.discard(icao)
        self.missing = {key for key in self.missing if key[0] != icao}

    @staticmethod
    def value(aircraft, name):
        field = aircraft["fields"].get(name, {})
        return field.get("value") if field.get("availability") == "PRESENT" else None

    def _consume(self, report, receipt, now):
        self.refresh_failed = report["status"] != "OK"
        if self.refresh_failed:
            return  # Retain last success only until its monotonic position expiry.
        self.last_success_receipt = receipt
        incoming = {ac["icao"]: ac for ac in report["aircraft"] if ac.get("icao")}
        for icao in set(self.aircraft) - set(incoming):
            self._remove(icao, now)
        anchors = {}
        receipt = report.get("request_finished_monotonic", receipt)
        latency = report.get("response_latency_seconds") or 0
        for icao, ac in incoming.items():
            pos = ac["fields"]["position"]
            age = pos.get("age_seconds")
            if pos.get("value") is None or age is None:
                continue
            # Absolute provider time is identity evidence ONLY, never local age.
            key = (report.get("provider_now_ms"), age,
                   pos["value"]["lat"], pos["value"]["lon"])
            previous = self.position_evidence.get(icao)
            anchors[icao] = previous if previous and previous[0] == key else (key, age + latency, receipt)
        self.aircraft, self.anchors = incoming, anchors
        # Retain evidence even after local expiry, bounded by this one snapshot.
        # A cached response must not resurrect an expired position on a new receipt.
        self.position_evidence = dict(anchors)
        self.processed.clear()

    def context(self, ac, observer, now, age, body):
        pos = self.value(ac, "position")
        gs, track = self.value(ac, "groundspeed"), self.value(ac, "ground_track")
        baro, geom = self.value(ac, "barometric_altitude"), self.value(ac, "geometric_altitude")
        if pos is None or gs is None or gs <= 0 or track is None or baro is None:
            raise ValueError("Missing required standalone motion fields")
        # Existing linear pressure correction, shared coefficient; never use nav_qnh.
        baro_amsl = (baro + (self.qnh - 1013.25) * QNH_CORRECTION_FT_PER_HPA) * .3048
        if geom is not None:
            altitude = geom * .3048 - self.geoid.undulation_m(pos["lat"], pos["lon"])
            source = "ADSBLOL_STANDALONE_GEOMETRIC_HAE_UNSPECIFIED_DERIVATION"
        else:
            altitude, source = baro_amsl, "ADSBLOL_STANDALONE_BARO_EXPLICIT_QNH"
        latitude, longitude = horizontal_position_from_t0(
            pos["lat"], pos["lon"], track, gs * 1.852, age)
        anchor = self.anchors.get(ac["icao"])
        fusion = None
        if anchor is not None:
            state = fuse_aircraft(
                ac["icao"], {}, ac,
                evaluated_at_utc=now.astimezone(UTC).isoformat().replace(
                    "+00:00", "Z"),
                evaluated_at_monotonic=self.monotonic(),
                receipt_monotonic=anchor[2],
                provider_health=("OK" if not self.refresh_failed
                                 else self.status))
            view = predictor_view(
                state, application_qnh_hpa=self.qnh, geoid=self.geoid)
            fusion = serialize_predictor_provenance(state, view)
        return ShadowEncounterContext(
            icao=ac["icao"], callsign=self.value(ac, "callsign") or "", body=body,
            prediction_base_utc=now, observer_context=observer,
            latitude_deg=latitude, longitude_deg=longitude, track_deg=track,
            groundspeed_kmh=gs * 1.852, current_altitude_m=baro_amsl,
            vertical_motion=None, vertical_intent=None, vertical_policy=current_vertical_prediction_policy(),
            qnh_hpa=self.qnh, geometric_altitude_correction_m=altitude - baro_amsl,
            altitude_source=source, position_source=self.source,
            track_source=self.source + "_UNKNOWN_AGE",
            aircraft_los_resolver=self.aircraft_los,
            body_position_resolver=self.body_position,
            fusion_provenance=fusion)

    def _publish(self, prediction, context, now):
        if not 0 < (prediction.predicted_transit_utc - now).total_seconds() <= self.config.horizon_seconds:
            self.dashboard.withdraw_source(prediction.icao, prediction.body, self, now,
                                           reason="PREDICTION_UNAVAILABLE")
            return
        candidate = DashboardCandidate(
            body=prediction.body, icao=prediction.icao, callsign=prediction.callsign,
            predicted_event_utc=prediction.predicted_transit_utc, separation_deg=prediction.separation_deg,
            body_azimuth_deg=prediction.body_azimuth_deg, body_elevation_deg=prediction.body_altitude_deg,
            aircraft_elevation_deg=prediction.aircraft_altitude_deg, distance_km=None,
            last_prediction_update_utc=now, telegram_range=False,
            transit_distance_km=prediction.slant_range_km, encounter_id=prediction.encounter_id,
            prediction_geometry="TRUE_2D",
            aircraft_source_mode=self.source_mode)
        self.dashboard.publish_authoritative(candidate, prediction, source_owner=self)

    def step(self):
        now, mono = self.now(), self.monotonic()
        observer = self.poller.observer.resolve(now)
        identity = scope(observer)
        if identity != self.identity:
            self._clear(now)
            self.identity = identity
        if observer.requested_mode not in ("STATIC", "MANUAL"):
            self.status = "PRIVACY_BLOCKED" if observer.requested_mode == "MOBILE" else "MANUAL_REQUIRED"
            self._diagnostics()
            return
        envelope = self.poller.snapshot()
        if envelope and envelope[0] != self.sequence and envelope[1] == identity:
            self.sequence = envelope[0]
            self._consume(envelope[2], envelope[3], now)
        for icao, ac in list(self.aircraft.items()):
            anchor = self.anchors.get(icao)
            age = anchor[1] + max(0, mono - anchor[2]) if anchor else None
            if age is None or age > self.max_age:
                self._remove(icao, now, reason="MOTION_STALE")
                continue
            if icao in self.processed:
                for body in ("SUN", "MOON"):
                    if (icao, body) not in self.missing:
                        continue
                    transition = self.lifecycle.unavailable_transition(observer.epoch, icao, body, now)
                    if transition.kind == AuthoritativeTransitionKind.WITHDRAWN:
                        self.dashboard.withdraw_source(icao, body, self, now,
                                                       reason="PREDICTION_UNAVAILABLE")
                continue
            with message_aircraft_cache():
                for body in ("SUN", "MOON"):
                    context = None
                    try:
                        context = self.context(ac, observer, now, age, body)
                        result = self.solver(context, self.config)
                    except Exception:
                        self.failures += 1
                        result = None
                    if scope(self.poller.observer.resolve(self.now())) != identity:
                        self._clear(self.now())
                        return
                    newest = self.poller.snapshot()
                    if newest and newest[0] != self.sequence:
                        return  # Latest-only: a completed newer snapshot wins.
                    if anchor[1] + max(0, self.monotonic() - anchor[2]) > self.max_age:
                        self._remove(icao, self.now(), reason="MOTION_STALE")
                        break
                    transition = (self.lifecycle.consider_transition(context, result, now) if context else
                                  self.lifecycle.unavailable_transition(observer.epoch, icao, body, now))
                    if transition.kind in (AuthoritativeTransitionKind.OPENED, AuthoritativeTransitionKind.UPDATED):
                        self.missing.discard((icao, body))
                        self._publish(transition.prediction, context, now)
                    else:
                        self.missing.add((icao, body))
                        if transition.kind == AuthoritativeTransitionKind.WITHDRAWN:
                            self.dashboard.withdraw_source(icao, body, self, now,
                                                       reason="PREDICTION_UNAVAILABLE")
            self.processed.add(icao)
        for body in ("SUN", "MOON"):
            position = self.body_position(body, now, observer.position)
            self.dashboard.update_body_position(body, position.altitude_deg, position.azimuth_deg, now)
        self.dashboard.tick(now)
        self._diagnostics()

    def _diagnostics(self):
        if self.identity and self.identity[0] in ("STATIC", "MANUAL"):
            recent = (self.last_success_receipt is not None and
                      self.monotonic() - self.last_success_receipt <= self.provider_stale_seconds)
            if recent:
                self.status = "REFRESHING" if self.poller.in_flight.locked() else "HEALTHY"
            elif self.refresh_failed:
                self.status = "ERROR"
            else:
                self.status = "STALE" if self.last_success_receipt is not None else "WAITING"
        self.dashboard.state.set_aircraft_source({
            "mode": self.source_mode, "requested_mode": self.source_mode,
            "effective_mode": self.source_mode, "provider": self.source,
            "status": self.status, "trust_policy": TRUST,
            "aircraft_count": len(self.aircraft), "max_position_age_seconds": self.max_age,
            "poll_seconds": self.poller.poll_seconds, "prediction_failures": self.failures})
        self.dashboard._publish_application_state()
