import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pytz

import transit_warning as transit
from environment import DailyEnvironmentRecorder, EnvironmentEvent, iter_environment_events
from metar import AwcMetar
from transit_clock import ReplayClock


class FakeClock:
    def __init__(self):
        self.current = datetime.datetime(2026, 8, 17, 12, 0, tzinfo=pytz.utc)

    def now_utc(self):
        return self.current

    def advance(self, seconds):
        self.current += datetime.timedelta(seconds=seconds)


class GetMetarPressTests(unittest.TestCase):
    def setUp(self):
        self.original_clock = transit.clock
        self.original_metar_t = transit.metar_t
        self.original_metar_attempt_t = transit.metar_attempt_t
        self.original_pressure = transit.pressure
        self.original_metar_station = transit.metar_station
        self.original_environment_recorder = transit.environment_recorder
        self.original_daily_environment_recorder = transit.daily_environment_recorder
        transit.clock = FakeClock()
        transit.metar_t = None
        transit.metar_attempt_t = None
        transit.pressure = 1013
        transit.metar_station = "EPRA"
        transit.environment_recorder = None
        transit.daily_environment_recorder = None

    def tearDown(self):
        transit.clock = self.original_clock
        transit.metar_t = self.original_metar_t
        transit.metar_attempt_t = self.original_metar_attempt_t
        transit.pressure = self.original_pressure
        transit.metar_station = self.original_metar_station
        transit.environment_recorder = self.original_environment_recorder
        transit.daily_environment_recorder = self.original_daily_environment_recorder

    def observation(self, altim=1015.0, age_seconds=0, station="EPRA", raw_ob="METAR"):
        return AwcMetar(
            icao_id=station,
            obs_time=transit.clock.now_utc() - datetime.timedelta(seconds=age_seconds),
            altim=altim,
            raw_ob=raw_ob,
        )

    def persisted_event(self, value=1004.0, days_ago=0):
        return EnvironmentEvent(
            1,
            transit.clock.now_utc() - datetime.timedelta(days=days_ago, minutes=1),
            "qnh",
            value,
            "awc",
            "EPRA",
            transit.clock.now_utc() - datetime.timedelta(days=days_ago, minutes=5),
        )

    def seed_daily_history(self, directory, event):
        recorder = DailyEnvironmentRecorder(event.time, directory, "EPRA")
        recorder.record_qnh(event)
        recorder.close()

    def test_initialization_recovers_today_without_enabling_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            event = self.persisted_event(1004.0)
            self.seed_daily_history(directory, event)
            recovered = transit.initialize_daily_environment(directory)
            try:
                self.assertEqual(recovered.value_hpa, 1004.0)
                self.assertEqual(transit.pressure, 1004.0)
                self.assertIsNone(transit.metar_t)
                self.assertIsNone(transit.metar_attempt_t)
            finally:
                transit.daily_environment_recorder.close()

    def test_initialization_recovers_previous_day_only(self):
        with tempfile.TemporaryDirectory() as directory:
            event = self.persisted_event(1005.0, days_ago=1)
            self.seed_daily_history(directory, event)
            recovered = transit.initialize_daily_environment(directory)
            try:
                self.assertEqual(recovered.value_hpa, 1005.0)
                self.assertEqual(transit.pressure, 1005.0)
            finally:
                transit.daily_environment_recorder.close()

    def test_initialization_ignores_older_history_and_uses_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            event = self.persisted_event(1002.0, days_ago=2)
            self.seed_daily_history(directory, event)
            self.assertIsNone(transit.initialize_daily_environment(directory))
            try:
                self.assertEqual(transit.pressure, 1013)
            finally:
                transit.daily_environment_recorder.close()

    @patch.object(transit, "DailyEnvironmentRecorder")
    def test_failed_daily_recorder_does_not_block_awc(self, recorder_type):
        failed_recorder = Mock(status=DailyEnvironmentRecorder.FAILED)
        failed_recorder.recover_recent_qnh.return_value = None
        recorder_type.return_value = failed_recorder
        transit.initialize_daily_environment()
        observation = self.observation(1006.0)
        with patch.object(transit, "fetch_awc_metar", return_value=observation) as fetch:
            self.assertEqual(transit.get_metar_press(), 1006.0)
        fetch.assert_called_once_with("EPRA")
        failed_recorder.record_qnh.assert_called_once()

    @patch.object(transit, "fetch_awc_metar")
    def test_immediate_awc_replaces_recovered_qnh_and_then_caches(self, fetch):
        with tempfile.TemporaryDirectory() as directory:
            self.seed_daily_history(directory, self.persisted_event(1004.0))
            transit.initialize_daily_environment(directory)
            fetch.return_value = self.observation(1006.0)
            try:
                self.assertEqual(transit.get_metar_press(), 1006.0)
                self.assertEqual(fetch.call_count, 1)
                transit.clock.advance(899.999)
                self.assertEqual(transit.get_metar_press(), 1006.0)
                self.assertEqual(fetch.call_count, 1)
            finally:
                transit.daily_environment_recorder.close()

    @patch.object(transit, "fetch_awc_metar", return_value=None)
    def test_failed_immediate_awc_keeps_recovered_qnh_and_retry_policy(self, fetch):
        with tempfile.TemporaryDirectory() as directory:
            self.seed_daily_history(directory, self.persisted_event(1004.0))
            transit.initialize_daily_environment(directory)
            try:
                self.assertEqual(transit.get_metar_press(), 1004.0)
                transit.clock.advance(59.999)
                self.assertEqual(transit.get_metar_press(), 1004.0)
                self.assertEqual(fetch.call_count, 1)
                transit.clock.advance(0.001)
                self.assertEqual(transit.get_metar_press(), 1004.0)
                self.assertEqual(fetch.call_count, 2)
            finally:
                transit.daily_environment_recorder.close()

    @patch.object(transit, "fetch_awc_metar", return_value=None)
    def test_failed_immediate_awc_without_history_keeps_fallback(self, fetch):
        with tempfile.TemporaryDirectory() as directory:
            transit.initialize_daily_environment(directory)
            try:
                self.assertEqual(transit.get_metar_press(), 1013)
                self.assertEqual(transit.pressure, 1013)
            finally:
                transit.daily_environment_recorder.close()

    @patch.object(transit, "fetch_awc_metar")
    def test_identical_awc_does_not_append_duplicate_after_restart(self, fetch):
        with tempfile.TemporaryDirectory() as directory:
            self.seed_daily_history(directory, self.persisted_event(1004.0))
            transit.initialize_daily_environment(directory)
            path = transit.daily_environment_recorder._path_for(transit.clock.now_utc().date())
            before = list(iter_environment_events(path))
            fetch.return_value = self.observation(1004.0)
            try:
                self.assertEqual(transit.get_metar_press(), 1004.0)
                after = list(iter_environment_events(path))
                self.assertEqual(len(after), len(before))
            finally:
                transit.daily_environment_recorder.close()

    @patch.object(transit, "fetch_awc_metar")
    def test_first_valid_epra_qnh_is_stored(self, fetch):
        fetch.return_value = self.observation()
        self.assertEqual(transit.get_metar_press(), 1015.0)
        self.assertEqual(transit.pressure, 1015.0)
        self.assertEqual(transit.metar_t, transit.clock.now_utc())
        self.assertEqual(transit.metar_attempt_t, transit.clock.now_utc())
        fetch.assert_called_once_with("EPRA")

    @patch.object(transit, "fetch_awc_metar")
    def test_accepted_awc_qnh_is_recorded_with_metadata(self, fetch):
        observation = self.observation(1011.8)
        fetch.return_value = observation
        recorder = Mock()
        transit.environment_recorder = recorder

        transit.get_metar_press()

        event = recorder.record.call_args.args[0]
        self.assertEqual(event.time, transit.clock.now_utc())
        self.assertEqual(event.value_hpa, 1011.8)
        self.assertEqual(event.source, "awc")
        self.assertEqual(event.station, "EPRA")
        self.assertEqual(event.obs_time, observation.obs_time)

    def test_configure_recording_writes_initial_fallback_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "environment.jsonl"
            transit.configure_environment_recording(path)
            recorder = transit.environment_recorder
            try:
                from environment import iter_environment_events
                event = next(iter_environment_events(path))
                self.assertEqual(event.time, transit.clock.now_utc())
                self.assertEqual(event.value_hpa, 1013.0)
                self.assertEqual(event.source, "fallback")
                self.assertEqual(event.station, "EPRA")
                self.assertIsNone(event.obs_time)
            finally:
                recorder.close()

    @patch.object(transit, "fetch_awc_metar")
    def test_kjfk_uses_altim_without_parsing_raw_a_value(self, fetch):
        transit.metar_station = "KJFK"
        fetch.return_value = self.observation(
            altim=1009.6, station="KJFK", raw_ob="KJFK 171151Z A2981"
        )
        self.assertEqual(transit.get_metar_press(), 1009.6)
        self.assertEqual(transit.pressure, 1009.6)

    @patch.object(transit, "fetch_awc_metar")
    def test_valid_qnh_cache_boundary(self, fetch):
        fetch.side_effect = (
            self.observation(1015.0),
            self.observation(1009.0),
            self.observation(1008.0),
        )
        self.assertEqual(transit.get_metar_press(), 1015.0)
        transit.clock.advance(899.999)
        self.assertEqual(transit.get_metar_press(), 1015.0)
        self.assertEqual(fetch.call_count, 1)
        transit.clock.advance(0.001)
        self.assertEqual(transit.get_metar_press(), 1009.0)
        self.assertEqual(fetch.call_count, 2)
        transit.clock.advance(900.001)
        self.assertEqual(transit.get_metar_press(), 1008.0)
        self.assertEqual(fetch.call_count, 3)

    @patch.object(transit, "fetch_awc_metar")
    def test_accepts_observation_age_89_59_999(self, fetch):
        fetch.return_value = self.observation(age_seconds=5399.999)
        self.assertEqual(transit.get_metar_press(), 1015.0)

    @patch.object(transit, "fetch_awc_metar")
    def test_accepts_observation_age_exactly_90_minutes(self, fetch):
        fetch.return_value = self.observation(age_seconds=5400)
        self.assertEqual(transit.get_metar_press(), 1015.0)

    @patch.object(transit, "fetch_awc_metar")
    def test_stale_observation_keeps_previous_qnh(self, fetch):
        transit.pressure = 1007.0
        transit.metar_t = transit.clock.now_utc() - datetime.timedelta(seconds=901)
        previous_metar_t = transit.metar_t
        fetch.return_value = self.observation(age_seconds=5400.001)
        self.assertEqual(transit.get_metar_press(), 1007.0)
        self.assertEqual(transit.pressure, 1007.0)
        self.assertEqual(transit.metar_t, previous_metar_t)

    @patch.object(transit, "fetch_awc_metar")
    def test_stale_first_observation_keeps_initial_pressure(self, fetch):
        fetch.return_value = self.observation(age_seconds=5400.001)
        self.assertEqual(transit.get_metar_press(), 1013)
        self.assertIsNone(transit.metar_t)

    @patch.object(transit, "fetch_awc_metar")
    def test_future_observation_is_rejected(self, fetch):
        transit.environment_recorder = Mock()
        fetch.return_value = self.observation(age_seconds=-0.001)
        self.assertEqual(transit.get_metar_press(), 1013)
        self.assertIsNone(transit.metar_t)
        transit.environment_recorder.record.assert_not_called()

    @patch.object(transit, "fetch_awc_metar", return_value=None)
    def test_provider_error_does_not_record_environment_event(self, fetch):
        transit.environment_recorder = Mock()
        self.assertEqual(transit.get_metar_press(), 1013)
        transit.environment_recorder.record.assert_not_called()

    @patch.object(transit, "fetch_awc_metar")
    def test_stale_observation_does_not_record_environment_event(self, fetch):
        transit.environment_recorder = Mock()
        fetch.return_value = self.observation(age_seconds=5400.001)
        self.assertEqual(transit.get_metar_press(), 1013)
        transit.environment_recorder.record.assert_not_called()

    @patch.object(transit, "fetch_awc_metar")
    def test_retry_after_stale_observation_uses_60_second_boundary(self, fetch):
        fetch.side_effect = (
            self.observation(age_seconds=5401),
            self.observation(1009.0),
        )
        self.assertEqual(transit.get_metar_press(), 1013)
        transit.clock.advance(59.999)
        self.assertEqual(transit.get_metar_press(), 1013)
        self.assertEqual(fetch.call_count, 1)
        transit.clock.advance(0.001)
        self.assertEqual(transit.get_metar_press(), 1009.0)
        self.assertEqual(fetch.call_count, 2)

    @patch.object(transit, "fetch_awc_metar")
    def test_retry_after_future_observation_uses_60_second_boundary(self, fetch):
        fetch.side_effect = (
            self.observation(age_seconds=-1),
            self.observation(1009.0),
        )
        self.assertEqual(transit.get_metar_press(), 1013)
        transit.clock.advance(60)
        self.assertEqual(transit.get_metar_press(), 1009.0)
        self.assertEqual(fetch.call_count, 2)

    @patch.object(transit, "fetch_awc_metar", return_value=None)
    def test_provider_failure_keeps_previous_qnh(self, fetch):
        transit.pressure = 1007.0
        transit.metar_t = transit.clock.now_utc() - datetime.timedelta(seconds=901)
        self.assertEqual(transit.get_metar_press(), 1007.0)
        self.assertEqual(transit.pressure, 1007.0)

    @patch.object(transit, "fetch_awc_metar")
    def test_replay_does_not_fetch_or_change_metar_state(self, fetch):
        replay_clock = ReplayClock()
        replay_clock.advance_to(transit.clock.now_utc())
        transit.clock = replay_clock
        transit.pressure = 1007.0
        original_metar_t = datetime.datetime(2026, 8, 17, 10, 0, tzinfo=pytz.utc)
        original_attempt_t = datetime.datetime(2026, 8, 17, 10, 1, tzinfo=pytz.utc)
        transit.metar_t = original_metar_t
        transit.metar_attempt_t = original_attempt_t
        transit.environment_recorder = Mock()
        self.assertEqual(transit.get_metar_press(), 1007.0)
        fetch.assert_not_called()
        self.assertEqual(transit.pressure, 1007.0)
        self.assertIs(transit.metar_t, original_metar_t)
        self.assertIs(transit.metar_attempt_t, original_attempt_t)
        transit.environment_recorder.record.assert_not_called()

    def test_replay_does_not_create_daily_environment_recorder(self):
        replay_clock = ReplayClock()
        transit.clock = replay_clock
        with patch.object(transit, "DailyEnvironmentRecorder") as recorder_type:
            self.assertIsNone(transit.initialize_daily_environment())
        recorder_type.assert_not_called()
        self.assertIsNone(transit.daily_environment_recorder)


if __name__ == "__main__":
    unittest.main()
