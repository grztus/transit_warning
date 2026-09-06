"""Synthetic v2 fixtures only; no network or production runtime imports."""

import copy
import datetime as dt
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from tools.adsblol_live import AdsbLolProvider, normalize_response, UTC
from tools.adsblol_live_probe import main


NOW = dt.datetime(2026, 9, 6, 12, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)


def fixture():
    return {"now": NOW_MS, "ctime": NOW_MS, "ptime": 1, "msg": "No error", "total": 1, "ac": [{
        "hex": "abc123", "flight": "TEST1   ", "lat": 12.3, "lon": 34.5,
        "alt_baro": 10000, "alt_geom": 10800, "gs": 250.5, "track": 359.8,
        "baro_rate": -512, "geom_rate": -448, "nav_altitude_mcp": 12000,
        "nav_altitude_fms": 14000, "nav_qnh": 1013.25, "mag_heading": 1.2,
        "true_heading": 2.3, "nav_heading": 3.4, "track_rate": -.1, "roll": -2,
        "squawk": "0042", "nic": 9, "rc": 75, "nac_p": 10, "nac_v": 2,
        "sil": 3, "sil_type": "perhour", "nic_baro": 1, "gva": 2, "sda": 2,
        "version": 2, "type": "adsb_icao", "mlat": [], "tisb": [],
        "seen": .2, "seen_pos": 2.5,
    }]}


def response(payload=None, status=200, headers=None):
    item = Mock(status_code=status, headers=headers or {})
    item.json.return_value = fixture() if payload is None else payload
    return item


def provider(session=None, **kwargs):
    return AdsbLolProvider(session=session or Mock(get=Mock(return_value=response())),
                           now=lambda: NOW + dt.timedelta(seconds=1),
                           monotonic=Mock(side_effect=range(100)), max_retries=0, **kwargs)


class NormalizationTests(unittest.TestCase):
    def normalize(self, payload=None, received=None):
        return normalize_response(payload if payload is not None else fixture(),
                                  received or NOW + dt.timedelta(seconds=1), 123)

    def test_all_required_fields_and_no_rounding_or_datum_substitution(self):
        normalized = self.normalize()
        ac = normalized["aircraft"][0]
        f = ac["fields"]
        expected = {"callsign": "TEST1", "barometric_altitude": 10000,
                    "geometric_altitude": 10800, "groundspeed": 250.5, "ground_track": 359.8,
                    "barometric_vertical_rate": -512, "geometric_vertical_rate": -448,
                    "selected_altitude_mcp": 12000, "selected_altitude_fms": 14000,
                    "selected_altimeter_setting": 1013.25, "heading_magnetic": 1.2,
                    "heading_true": 2.3, "selected_heading": 3.4, "track_rate": -.1,
                    "roll": -2, "squawk": "0042", "nic": 9, "nac_p": 10, "nac_v": 2,
                    "sil": 3, "gva": 2, "aircraft_source_type": "adsb_icao"}
        for name, value in expected.items():
            self.assertEqual(f[name]["value"], value)
            self.assertEqual(f[name]["source"], "ADSBLOL")
            self.assertIsNone(f[name]["confidence"])
        self.assertEqual(ac["icao"], "ABC123")
        self.assertEqual(f["position"]["value"], {"lat": 12.3, "lon": 34.5})
        self.assertEqual(f["geometric_altitude"]["datum_or_reference"], "WGS84_HAE")
        self.assertEqual(f["geometric_altitude"]["provenance"]["derivation"], "UNSPECIFIED")
        self.assertEqual(f["geometric_altitude"]["provenance"]["datum_basis"], "PROVIDER_DECLARED")
        self.assertEqual(f["barometric_altitude"]["datum_or_reference"], "PRESSURE")
        self.assertEqual(f["geometric_altitude"]["quality"]["field_refs"], ["gva"])
        json.dumps(normalized, allow_nan=False)

    def test_position_age_only_and_message_age_is_not_field_freshness(self):
        ac = self.normalize()["aircraft"][0]
        for name, field in ac["fields"].items():
            if name == "position":
                self.assertEqual(field["age_seconds"], 2.5)
                self.assertEqual(field["freshness_state"], "KNOWN")
                self.assertEqual(field["observed_at_utc"], "2026-09-06T11:59:57.500000Z")
            else:
                self.assertIsNone(field["age_seconds"], name)
                self.assertIsNone(field["observed_at_utc"], name)
        self.assertEqual(ac["fields"]["aircraft_message_age"]["value"], .2)
        self.assertEqual(ac["apparent_position_age_at_receipt_seconds"], 3.5)
        for name in ("ground_track", "barometric_altitude", "geometric_altitude", "groundspeed"):
            self.assertEqual(ac["fields"][name]["freshness_state"], "UNKNOWN")

    def test_missing_null_invalid_and_ground_are_distinct_from_zero(self):
        payload = fixture()
        ac = payload["ac"][0]
        del ac["alt_geom"]
        ac.update(alt_baro="ground", gs=0, geom_rate=0, track="bad", roll=None)
        f = self.normalize(payload)["aircraft"][0]["fields"]
        self.assertEqual(f["geometric_altitude"]["missing_reason"], "ABSENT")
        self.assertEqual(f["roll"]["missing_reason"], "NULL")
        self.assertEqual(f["ground_track"]["availability"], "INVALID")
        self.assertEqual(f["barometric_altitude"]["state"], "GROUND")
        self.assertEqual(f["barometric_altitude"]["availability"], "NOT_APPLICABLE")
        self.assertIsNone(f["barometric_altitude"]["value"])
        self.assertEqual(f["groundspeed"]["value"], 0)
        self.assertEqual(f["geometric_vertical_rate"]["value"], 0)

    def test_non_icao_and_source_aliases_remain_separate(self):
        payload = fixture()
        payload["ac"][0].update(hex="~abc123", mlat=["lat", "lon", "altitude", "callsign"], tisb=["gs"])
        ac = self.normalize(payload)["aircraft"][0]
        self.assertIsNone(ac["icao"])
        self.assertEqual(ac["aircraft_address"], "~ABC123")
        for name in ("position", "barometric_altitude", "callsign"):
            self.assertEqual(ac["fields"][name]["provenance"]["field_source_flags"], ["MLAT"])
        self.assertEqual(ac["fields"]["groundspeed"]["provenance"]["field_source_flags"], ["TISB"])
        self.assertEqual(ac["fields"]["ground_track"]["provenance"]["field_source_flags"], [])

    def test_incomplete_position_does_not_use_historical_or_rough_coordinates(self):
        payload = fixture()
        del payload["ac"][0]["lon"]
        payload["ac"][0].update(lastPosition={"lat": 1, "lon": 2}, rr_lat=1, rr_lon=2, future_field="kept")
        ac = self.normalize(payload)["aircraft"][0]
        self.assertIsNone(ac["fields"]["position"]["value"])
        self.assertEqual(ac["fields"]["position"]["freshness_state"], "UNAVAILABLE")
        self.assertEqual(ac["extensions"]["future_field"], "kept")
        self.assertIn("lastPosition", ac["extensions"])

    def test_bad_position_age_never_borrows_seen(self):
        for age in (None, -1, "2", True):
            payload = fixture()
            payload["ac"][0]["seen_pos"] = age
            f = self.normalize(payload)["aircraft"][0]["fields"]["position"]
            self.assertEqual(f["freshness_state"], "UNKNOWN")
            self.assertIsNone(f["age_seconds"])

    def test_cached_observation_time_does_not_refresh_with_receipt_and_clock_skew_is_visible(self):
        first = self.normalize()
        later = self.normalize(received=NOW + dt.timedelta(seconds=20))
        self.assertEqual(first["snapshot_id"], later["snapshot_id"])
        a, b = (r["aircraft"][0]["fields"]["position"] for r in (first, later))
        self.assertEqual(a["observed_at_utc"], b["observed_at_utc"])
        self.assertNotEqual(a["received_at_utc"], b["received_at_utc"])
        skew = self.normalize(received=NOW - dt.timedelta(seconds=10))["aircraft"][0]
        self.assertLess(skew["apparent_position_age_at_receipt_seconds"], 0)
        self.assertIn("PROVIDER_SNAPSHOT_IN_FUTURE_OR_CLOCK_SKEW", skew["warnings"])

    def test_malformed_envelopes_and_seconds_not_milliseconds(self):
        for change in ({"now": NOW.timestamp()}, {"ctime": None}, {"total": 2},
                       {"ac": {}}, {"ac": [None]}, {"msg": "error"}, {"ptime": -1},
                       {"now": float("nan")}):
            payload = fixture()
            payload.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.normalize(payload)
        for value in (None, [], {"error": "upstream"}):
            with self.assertRaises(ValueError):
                self.normalize(value if value is not None else [])

    def test_empty_array_is_success_and_input_is_detached(self):
        payload = fixture()
        original = copy.deepcopy(payload)
        normalized = self.normalize(payload)
        normalized["aircraft"][0]["fields"]["position"]["value"]["lat"] = 99
        self.assertEqual(payload, original)
        payload.update(ac=[], total=0)
        self.assertEqual(self.normalize(payload)["aircraft"], [])


class ProviderTests(unittest.TestCase):
    def test_geographic_query_metadata_tls_and_radius_cap(self):
        session = Mock(get=Mock(return_value=response()))
        report = provider(session).acquire(observer_mode="MANUAL", lat=12.3, lon=34.5, radius_nm=999)
        self.assertEqual(report["status"], "OK")
        self.assertEqual(report["query"]["radius_nm"], 250)
        self.assertTrue(report["query"]["radius_capped"])
        self.assertEqual(report["response_latency_seconds"], 1)
        self.assertEqual(report["http_status"], 200)
        self.assertEqual(report["aircraft_count"], 1)
        self.assertIsNone(report["provider_api_version"])
        args, kwargs = session.get.call_args
        self.assertEqual(args[0], "https://api.adsb.lol/v2/point/12.3/34.5/250")
        self.assertTrue(kwargs["verify"])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"], 10)
        session.get.return_value.close.assert_called_once()

    def test_mobile_is_blocked_before_inspecting_coordinates_or_network(self):
        session = Mock()
        for mode in ("MOBILE", "mobile", "MOBILE_STATIC_FALLBACK"):
            report = provider(session).acquire(observer_mode=mode, lat=object(), lon=object())
            self.assertEqual(report["status"], "PRIVACY_BLOCKED")
            self.assertIsNone(report["query"])
            self.assertEqual(report["attempts"], [])
            self.assertNotIn("center", json.dumps(report))
        session.get.assert_not_called()

    def test_invalid_mode_query_and_icao(self):
        session = Mock()
        for options in ({"observer_mode": "UNKNOWN"}, {"observer_mode": "STATIC", "lat": 91, "lon": 1},
                        {"observer_mode": "STATIC", "lat": 1, "lon": 1, "radius_nm": 2.5},
                        {"observer_mode": "STATIC", "lat": 1, "lon": 1, "radius_nm": -1},
                        {"observer_mode": "STATIC", "lat": 1, "lon": 1, "radius_nm": 10 ** 1000},
                        {"observer_mode": "STATIC", "icao": "../bad"}):
            self.assertEqual(provider(session).acquire(**options)["status"], "INVALID_QUERY")
        session.get.assert_not_called()

    def test_icao_query_does_not_send_a_center(self):
        session = Mock(get=Mock(return_value=response()))
        report = provider(session).acquire(observer_mode="STATIC", icao="abc123")
        self.assertEqual(report["query"], {"kind": "ICAO", "icao": "ABC123"})
        self.assertEqual(session.get.call_args.args[0], "https://api.adsb.lol/v2/icao/ABC123")

    def test_transport_errors_are_explicit_and_do_not_leak_exception_urls(self):
        for error, status in ((requests.exceptions.Timeout, "TIMEOUT"),
                              (requests.exceptions.SSLError, "TLS_ERROR"),
                              (requests.exceptions.ConnectionError, "NETWORK_ERROR")):
            session = Mock(get=Mock(side_effect=error("sensitive-url")))
            report = provider(session).acquire(observer_mode="STATIC", lat=1, lon=2)
            self.assertEqual(report["status"], status)
            self.assertNotIn("sensitive-url", json.dumps(report))
            self.assertIsNone(report["http_status"])
            self.assertEqual(report["aircraft"], [])

    def test_http_and_malformed_json_errors_not_empty_success(self):
        for code, status in ((401, "AUTH_ERROR"), (403, "AUTH_ERROR"), (422, "HTTP_ERROR"),
                             (429, "RATE_LIMITED"), (500, "HTTP_ERROR"), (302, "HTTP_ERROR")):
            report = provider(Mock(get=Mock(return_value=response(status=code)))).acquire(
                observer_mode="STATIC", lat=1, lon=2)
            self.assertEqual(report["status"], status)
        for payload in ({"error": "bad upstream"}, [], {"ac": []}):
            report = provider(Mock(get=Mock(return_value=response(payload)))).acquire(
                observer_mode="STATIC", lat=1, lon=2)
            self.assertEqual(report["status"], "MALFORMED_RESPONSE")
        invalid = response()
        invalid.json.side_effect = ValueError("bad JSON")
        self.assertEqual(provider(Mock(get=Mock(return_value=invalid))).acquire(
            observer_mode="STATIC", lat=1, lon=2)["status"], "MALFORMED_RESPONSE")

    def test_bounded_retry_after_and_no_retry_for_auth_or_tls(self):
        cancel = Mock()
        cancel.is_set.return_value = False
        cancel.wait.return_value = False
        session = Mock(get=Mock(side_effect=[response(status=429, headers={"Retry-After": "20"}), response()]))
        client = AdsbLolProvider(session=session, max_retries=1, cancel_event=cancel, jitter=lambda: 0)
        report = client.acquire(observer_mode="STATIC", lat=1, lon=2)
        self.assertEqual(report["status"], "OK")
        self.assertEqual(len(report["attempts"]), 2)
        cancel.wait.assert_called_once_with(20)
        for reply in (response(status=401), response(status=429, headers={"Retry-After": "120"})):
            session.get.reset_mock()
            session.get.side_effect = None
            session.get.return_value = reply
            report = client.acquire(observer_mode="STATIC", lat=1, lon=2)
            self.assertEqual(session.get.call_count, 1)
        self.assertEqual(report["retry_after_seconds"], 120)

    def test_cancel_during_request_discards_data(self):
        cancel = Mock()
        cancel.is_set.side_effect = [False, True]
        report = provider(cancel_event=cancel).acquire(observer_mode="STATIC", lat=1, lon=2)
        self.assertEqual(report["status"], "CANCELLED")
        self.assertEqual(report["aircraft"], [])

    def test_retry_budget_exhaustion_tls_and_cancelled_backoff(self):
        cancel = Mock()
        cancel.is_set.return_value = False
        cancel.wait.return_value = False
        session = Mock(get=Mock(side_effect=requests.exceptions.Timeout("private-url")))
        client = AdsbLolProvider(session=session, max_retries=2, cancel_event=cancel, jitter=lambda: 0)
        report = client.acquire(observer_mode="STATIC", lat=1, lon=2)
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual(report["status"], "TIMEOUT")
        self.assertEqual([call.args[0] for call in cancel.wait.call_args_list], [10, 20])
        session.get.reset_mock()
        session.get.side_effect = requests.exceptions.SSLError("private-url")
        report = client.acquire(observer_mode="STATIC", lat=1, lon=2)
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(report["status"], "TLS_ERROR")
        session.get.reset_mock()
        session.get.side_effect = requests.exceptions.Timeout()
        cancel.wait.return_value = True
        report = client.acquire(observer_mode="STATIC", lat=1, lon=2)
        self.assertEqual(report["status"], "CANCELLED")
        self.assertEqual(session.get.call_count, 1)

    def test_retry_after_http_date(self):
        session = Mock(get=Mock(return_value=response(
            status=429, headers={"Retry-After": "Sun, 06 Sep 2026 12:02:01 GMT"})))
        report = provider(session).acquire(observer_mode="STATIC", lat=1, lon=2)
        self.assertEqual(report["retry_after_seconds"], 120)


class CliTests(unittest.TestCase):
    def test_one_shot_json_output_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "diagnostic.json"
            with patch("tools.adsblol_live.requests.get", return_value=response()) as get, patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(main(["--lat", "1", "--lon", "2", "--json-output", str(path)]), 0)
                self.assertEqual(get.call_count, 1)
                self.assertIn("freshness=UNKNOWN", out.getvalue())
                self.assertIn("WGS84_HAE", out.getvalue())
                self.assertTrue(json.loads(path.read_text())["diagnostic_only"])
                with self.assertRaises(SystemExit):
                    main(["--lat", "1", "--lon", "2", "--json-output", str(path)])
                self.assertEqual(get.call_count, 1)

    def test_mobile_cli_does_not_print_or_send_query_position(self):
        with patch("tools.adsblol_live.requests.get") as get, patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(main(["--observer-mode", "MOBILE", "--lat", "12.3456", "--lon", "34.5678"]), 1)
            get.assert_not_called()
            self.assertIn("PRIVACY_BLOCKED", out.getvalue())
            self.assertNotIn("12.3456", out.getvalue())
            self.assertNotIn("34.5678", out.getvalue())

    def test_import_does_not_load_prediction_dashboard_or_recorder(self):
        script = ("import sys; import tools.adsblol_live_probe; "
                  "assert not any(n in sys.modules for n in "
                  "('transit_warning','transit_centerline','live_dashboard','recording'))")
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                cwd=Path(__file__).resolve().parents[1], timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_finite_polling_preserves_query_and_respects_retry_after(self):
        event = Mock()
        event.is_set.return_value = False
        with patch("tools.adsblol_live_probe.threading.Event", return_value=event), patch(
                "tools.adsblol_live.requests.get", side_effect=[
                    response(status=429, headers={"Retry-After": "25"}), response()]) as get, patch(
                "sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(["--icao", "abc123", "--count", "2", "--retries", "0"]), 1)
            self.assertEqual(get.call_count, 2)
            self.assertEqual(get.call_args_list[0].args, get.call_args_list[1].args)
            event.wait.assert_called_once_with(25)

    def test_poll_input_validation_before_network(self):
        with patch("tools.adsblol_live.requests.get") as get, patch("sys.stderr", new_callable=io.StringIO):
            for options in (["--count", "0"], ["--poll-seconds", "nan"], ["--poll-seconds", "1"]):
                with self.assertRaises(SystemExit):
                    main(["--icao", "ABC123"] + options)
            get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
