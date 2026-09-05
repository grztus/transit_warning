import dataclasses
import datetime
import json
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

import pytz

import transit_warning as transit
from config import InstallationConfig
from observer_position import (
    ObserverContext,
    ObserverPosition,
    RuntimeObserverPositionProvider,
    StaticObserverPositionProvider,
)
from live_dashboard import MobileGpsState


NOW = datetime.datetime(2026, 8, 30, 12, 0, tzinfo=pytz.utc)

CONFIG = InstallationConfig(
    observer_lat=50.0,
    observer_lon=20.0,
    observer_elevation_m=200.0,
    transition_altitude_ft=6500,
    adsb_host="127.0.0.1",
    adsb_port=30003,
    adsb_timestamp_timezone="Europe/Warsaw",
    mlat_host="127.0.0.1",
    mlat_port=30106,
    metar_station="EPRA",
)


class ObserverPositionTests(unittest.TestCase):
    def setUp(self):
        transit.apply_installation_config(CONFIG)

    def test_position_is_immutable_and_static_provider_returns_same_instance(self):
        position = ObserverPosition(50.0, 20.0, 200.0)
        provider = StaticObserverPositionProvider(position)
        self.assertIs(position, provider.current())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            position.latitude_deg = 51.0

    def test_body_ephemeris_uses_explicit_observer(self):
        explicit = ObserverPosition(49.0, 19.0, 300.0)
        expected = transit.body_position_at_utc("moon", NOW, explicit)
        transit.my_lat = -10.0
        transit.my_lon = -20.0
        transit.my_elevation_const = -100.0
        actual = transit.body_position_at_utc("moon", NOW, explicit)
        self.assertEqual(expected, actual)

    def test_moving_solver_passes_one_context_to_ephemeris_and_geometry(self):
        position = ObserverPosition(50.0, 20.0, 200.0)
        result = (50.1, 20.1, 100.0, 20.1, 10.0, 20.0,
                  10.0, 0, 100.0, 20.0, NOW)
        with patch.object(
                transit, "body_position_at_utc",
                return_value=transit.BodyPosition(20.0, 100.0, 1800.0, NOW)
        ) as ephemeris, patch.object(
                transit, "transit_pred", side_effect=[result, result]
        ) as geometry:
            transit.moving_body_transit_pred(
                "moon", position, (50.1, 20.1), 180.0, 800.0,
                10000.0, NOW)
        self.assertTrue(all(call.args[2] is position
                            for call in ephemeris.call_args_list))
        self.assertTrue(all(call.kwargs["observer_position"] is position
                            for call in geometry.call_args_list))

    def test_aircraft_geometry_uses_explicit_observer(self):
        position = ObserverPosition(50.0, 20.0, 200.0)
        first = transit.angular_position_from_observer(
            position.coordinates, position.elevation_m,
            (50.1, 20.1), 10000.0)
        transit.my_lat = -10.0
        transit.my_lon = -20.0
        transit.my_elevation_const = -100.0
        second = transit.angular_position_from_observer(
            position.coordinates, position.elevation_m,
            (50.1, 20.1), 10000.0)
        self.assertEqual(first, second)

    def test_mobile_policy_uses_static_elevation_and_last_known_fix(self):
        mobile = MobileGpsState(enabled=True, fresh_seconds=15)
        provider = RuntimeObserverPositionProvider(
            ObserverPosition(50.0, 20.0, 200.0), mode="MOBILE",
            fresh_seconds=15, fallback_enabled=False)
        provider.attach_mobile_state(mobile)
        self.assertEqual("MOBILE_NO_FIX", provider.resolve(NOW).effective_source)
        mobile.update({
            "latitude": 51.0, "longitude": 21.0, "accuracy": 4.0,
            "altitude": 999.0, "altitudeAccuracy": 10.0,
            "timestamp": 1.0,
        }, NOW)
        fresh = provider.resolve(NOW)
        self.assertEqual("MOBILE_FRESH", fresh.effective_source)
        self.assertEqual(200.0, fresh.position.elevation_m)
        stale = provider.resolve(NOW + datetime.timedelta(seconds=16))
        self.assertEqual("MOBILE_LAST_KNOWN", stale.effective_source)
        self.assertEqual(fresh.position, stale.position)

    def test_mobile_fallback_and_automatic_recovery(self):
        mobile = MobileGpsState(enabled=True, fresh_seconds=15)
        provider = RuntimeObserverPositionProvider(
            ObserverPosition(50.0, 20.0, 200.0), mode="MOBILE",
            fresh_seconds=15, fallback_enabled=True)
        provider.attach_mobile_state(mobile)
        self.assertEqual("STATIC_FALLBACK", provider.resolve(NOW).effective_source)
        mobile.update({
            "latitude": 51.0, "longitude": 21.0, "accuracy": 4.0,
            "altitude": None, "altitudeAccuracy": None, "timestamp": 1.0,
        }, NOW)
        self.assertEqual("MOBILE_FRESH", provider.resolve(NOW).effective_source)
        self.assertEqual("STATIC_FALLBACK", provider.resolve(
            NOW + datetime.timedelta(seconds=16)).effective_source)

    def test_manual_mode_is_effective_and_survives_other_modes(self):
        static = ObserverPosition(50.0, 20.0, 200.0)
        manual = ObserverPosition(51.25, 21.5, 315.0)
        provider = RuntimeObserverPositionProvider(static, manual_position=manual)
        self.assertEqual("STATIC", provider.resolve(NOW).effective_source)
        provider.set_mode("MANUAL", NOW)
        context = provider.resolve(NOW)
        self.assertEqual("MANUAL", context.requested_mode)
        self.assertEqual("MANUAL", context.effective_source)
        self.assertEqual(manual, context.position)
        provider.set_mode("STATIC", NOW)
        self.assertEqual(static, provider.resolve(NOW).position)
        provider.set_mode("MANUAL", NOW)
        self.assertEqual(manual, provider.resolve(NOW).position)

    def test_snapshot_uses_explicit_prediction_observer(self):
        manager = Mock()
        old_manager = transit.transit_snapshot_manager
        transit.transit_snapshot_manager = manager
        explicit = ObserverPosition(49.0, 19.0, 300.0)
        solver_input = {
            "aircraft_altitude_m": 10000.0, "groundspeed": 800.0,
            "track": 180.0, "aircraft_lat": 49.1, "aircraft_lon": 19.1,
        }
        result = (49.1, 19.1, 100.0, 20.1, 10.0, 20.0,
                  10.0, 0, 100.0, 20.0, NOW)
        transit.moon_predicted_transit_utc["ABC123"] = (
            NOW + datetime.timedelta(seconds=10))
        try:
            with patch.object(transit, "build_frozen_prediction_state",
                              return_value={}):
                transit.capture_transit_prediction(
                    "ABC123", "TEST", "moon", result, NOW, solver_input,
                    explicit)
            payload = manager.consider_prediction.call_args.args[0]
            self.assertEqual(
                {"lat": explicit.latitude_deg,
                 "lon": explicit.longitude_deg,
                 "elevation_m": explicit.elevation_m},
                payload["observer"])
        finally:
            transit.transit_snapshot_manager = old_manager
            transit.moon_predicted_transit_utc.pop("ABC123", None)

    def test_mobile_snapshot_records_source_without_mobile_coordinates(self):
        manager = Mock()
        old_manager = transit.transit_snapshot_manager
        transit.transit_snapshot_manager = manager
        explicit = ObserverPosition(49.0, 19.0, 300.0)
        context = ObserverContext(
            explicit, "MOBILE", "MOBILE_FRESH", 2.0, 5.0, False, False, 4)
        result = (49.1, 19.1, 100.0, 20.1, 10.0, 20.0,
                  10.0, 0, 100.0, 20.0, NOW)
        solver_input = {"aircraft_altitude_m": 10000.0,
                        "groundspeed": 800.0, "track": 180.0,
                        "aircraft_lat": 49.1, "aircraft_lon": 19.1}
        transit.moon_predicted_transit_utc["ABC123"] = (
            NOW + datetime.timedelta(seconds=10))
        try:
            with patch.object(transit, "build_frozen_prediction_state",
                              return_value={}):
                transit.capture_transit_prediction(
                    "ABC123", "TEST", "moon", result, NOW, solver_input,
                    explicit, context)
            payload = manager.consider_prediction.call_args.args[0]
            self.assertEqual("MOBILE_FRESH", payload[
                "observer_context"]["effective_source"])
            self.assertNotIn("lat", payload["observer"])
            self.assertNotIn("lon", payload["observer"])
        finally:
            transit.transit_snapshot_manager = old_manager
            transit.moon_predicted_transit_utc.pop("ABC123", None)

    def test_dashboard_and_telegram_payloads_do_not_gain_observer_fields(self):
        dashboard_source = Path("live_dashboard.py").read_text(encoding="utf-8")
        telegram_source = Path("telegram_notifications.py").read_text(
            encoding="utf-8")
        encoded_telegram = json.dumps(telegram_source).lower()
        self.assertNotIn("observer_position", encoded_telegram)
        self.assertNotIn("latitude_deg", encoded_telegram)
        self.assertNotIn("latitude_deg", json.dumps(dashboard_source).lower())


if __name__ == "__main__":
    unittest.main()
