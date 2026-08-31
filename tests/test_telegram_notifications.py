import datetime
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from config import ConfigurationError, InstallationConfig, load_installation_config
import telegram_notifications as telegram
import transit_warning as transit


UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def event(body="MOON", seconds=42, separation=0.31):
    return telegram.TransitNotification(
        created_at_utc=NOW, body=body, icao="4BAB26", callsign="THY7DB",
        predicted_transit_utc=NOW + datetime.timedelta(seconds=seconds),
        time_to_event_seconds=seconds, separation_deg=separation,
        body_azimuth_deg=88.0, body_altitude_deg=6.2,
        aircraft_altitude_deg=6.51, distance_km=100.0)


class FakeTransport:
    def __init__(self, result=(True, None)):
        self.result = result
        self.messages = []
        self.sent = threading.Event()

    def send(self, text):
        self.messages.append(text)
        self.sent.set()
        return self.result


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"ok":true,"result":{}}'


class TelegramNotifierTests(unittest.TestCase):
    def test_disabled_is_exact_noop(self):
        notifier = telegram.create_telegram_notifier(False, "", "")
        self.assertFalse(notifier.notify(event()))

    def test_successful_message_and_duplicate_are_sent_once(self):
        transport = FakeTransport()
        notifier = telegram.TelegramNotifier(transport)
        try:
            self.assertTrue(notifier.notify(event()))
            self.assertFalse(notifier.notify(event(seconds=41)))
            self.assertTrue(transport.sent.wait(1))
        finally:
            notifier.close()
        self.assertEqual(1, len(transport.messages))

    def test_https_transport_posts_chat_and_message_with_timeout(self):
        captured = {}

        def open_request(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = request.data.decode("utf-8")
            captured["timeout"] = timeout
            return FakeResponse()

        transport = telegram.TelegramTransport(
            "fake-token", "fake-chat", timeout=3.0, opener=open_request)
        self.assertEqual((True, None), transport.send("hello"))
        self.assertTrue(captured["url"].startswith("https://api.telegram.org/"))
        self.assertIn("chat_id=fake-chat", captured["body"])
        self.assertIn("text=hello", captured["body"])
        self.assertEqual(3.0, captured["timeout"])

    def test_network_failure_is_fail_open_and_token_is_not_logged(self):
        errors = []
        secret = "123456:VERY_SECRET_TOKEN"

        def failing_open(request, timeout):
            raise OSError("connection failed for {}".format(secret))

        transport = telegram.TelegramTransport(
            secret, "123", opener=failing_open)
        success, error = transport.send("test")
        self.assertFalse(success)
        self.assertNotIn(secret, error)
        notifier = telegram.TelegramNotifier(
            FakeTransport((False, error)), errors.append)
        try:
            self.assertTrue(notifier.notify(event()))
            self.assertTrue(notifier._transport.sent.wait(1))
        finally:
            notifier.close()
        self.assertTrue(errors)
        self.assertNotIn(secret, " ".join(errors))

    def test_sun_and_moon_formatting(self):
        sun = telegram.format_transit_notification(event("SUN"))
        moon = telegram.format_transit_notification(event("MOON"))
        self.assertIn("☀️ SUN — potential transit", sun)
        self.assertIn("🌙 MOON — potential transit", moon)
        self.assertIn("in 42 s", moon)
        self.assertIn("SEP: 0.31°", moon)
        self.assertNotIn("latitude", moon.lower())

    def test_missing_enabled_configuration_fails_safely(self):
        with self.assertRaisesRegex(ValueError, "require"):
            telegram.create_telegram_notifier(True, "", "")


class TelegramConfigTests(unittest.TestCase):
    BASE = {
        "OBSERVER_LAT": "50", "OBSERVER_LON": "20",
        "OBSERVER_ELEVATION_M": "200", "TRANSITION_ALTITUDE_FT": "6500",
        "ADSB_TIMESTAMP_TIMEZONE": "UTC", "METAR_STATION": "EPRA",
    }

    def load(self, values):
        with patch("config.dotenv_values", return_value={}):
            return load_installation_config(values)

    def test_defaults_disabled_and_empty(self):
        config = self.load(self.BASE)
        self.assertFalse(config.telegram_notifications_enabled)
        self.assertEqual("", config.telegram_bot_token)
        self.assertEqual("", config.telegram_chat_id)

    def test_enabled_requires_token_and_chat(self):
        with self.assertRaises(ConfigurationError) as caught:
            self.load({**self.BASE, "TELEGRAM_NOTIFICATIONS_ENABLED": "true"})
        message = str(caught.exception)
        self.assertIn("TELEGRAM_BOT_TOKEN", message)
        self.assertIn("TELEGRAM_CHAT_ID", message)


class TransitIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.old_notifier = transit.telegram_notifier

    def tearDown(self):
        transit.telegram_notifier = self.old_notifier

    def test_emit_uses_existing_three_degree_condition_and_is_fail_open(self):
        notifier = Mock()
        notifier.notify.side_effect = RuntimeError("network")
        transit.telegram_notifier = notifier
        result = (51, 21, 88, 6.5, 10, 100, 42, 0, 88, 6.2, NOW)
        self.assertFalse(transit.emit_transit_notification(
            "4BAB26", "THY7DB", "moon", result, NOW, 100))
        notifier.notify.assert_called_once()
        notifier.reset_mock()
        outside = list(result)
        outside[3] = 10.0
        self.assertFalse(transit.emit_transit_notification(
            "4BAB26", "THY7DB", "moon", tuple(outside), NOW, 100))
        notifier.notify.assert_not_called()

    def test_disabled_notification_path_does_not_change_prediction_result(self):
        old_geometry = (
            transit.my_lat, transit.my_lon, transit.my_elevation_const)
        transit.my_lat, transit.my_lon, transit.my_elevation_const = (
            51.0, 21.0, 200.0)
        fixed_clock = SimpleNamespace(now_utc=lambda: NOW)
        try:
            with patch.object(transit, "clock", fixed_clock):
                before = transit.transit_pred(
                    (51.0, 21.0), (51.5, 21.5), 200, 800,
                    10986.668, 10, 180)
                transit.telegram_notifier = telegram.DisabledTelegramNotifier()
                after = transit.transit_pred(
                    (51.0, 21.0), (51.5, 21.5), 200, 800,
                    10986.668, 10, 180)
        finally:
            (transit.my_lat, transit.my_lon,
             transit.my_elevation_const) = old_geometry
        self.assertEqual(before, after)

    def test_test_notification_exits_before_threads_or_prediction_loop(self):
        configuration = InstallationConfig(
            observer_lat=50, observer_lon=20, observer_elevation_m=200,
            transition_altitude_ft=6500, adsb_host="a", adsb_port=30003,
            adsb_timestamp_timezone="UTC", mlat_host="m", mlat_port=30106,
            metar_station="EPRA", telegram_notifications_enabled=True,
            telegram_bot_token="fake-token", telegram_chat_id="fake-chat")
        notifier = Mock()
        notifier.send_test.return_value = True
        args = SimpleNamespace(
            clock="real", record=False, test_notification=True,
            environment_replay=None, environment_record=None,
            raw_diagnostics_replay=None)
        with (patch.object(transit, "load_installation_config",
                           return_value=configuration),
              patch.object(transit, "runtime_args", args),
              patch.object(transit, "create_telegram_notifier",
                           return_value=notifier),
              patch.object(transit.threading, "Thread") as thread):
            transit.main()
        notifier.send_test.assert_called_once()
        notifier.close.assert_called_once()
        thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
