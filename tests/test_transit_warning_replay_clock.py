import datetime
import unittest

import pytz

import transit_warning as transit
from config import InstallationConfig
from transit_clock import RealClock, ReplayClock, clock_from_args


TEST_CONFIG = InstallationConfig(
    observer_lat=51.1111,
    observer_lon=21.1111,
    observer_elevation_m=111.0,
    adsb_host="127.0.0.1",
    adsb_port=30003,
    mlat_host="127.0.0.1",
    mlat_port=30106,
    metar_url="https://weather.example/metar",
)


def message(generated, logged, icao="ABC123"):
    generated_date, generated_time = generated.split()
    logged_date, logged_time = logged.split()
    return "MSG,1,1,1,{icao},1,{gd},{gt},{ld},{lt},TEST123".format(
        icao=icao,
        gd=generated_date,
        gt=generated_time,
        ld=logged_date,
        lt=logged_time,
    )


def utc(value):
    return datetime.datetime.strptime(value, "%Y/%m/%d %H:%M:%S.%f").replace(tzinfo=pytz.utc)


class ProcessLineReplayClockTests(unittest.TestCase):
    def setUp(self):
        transit.apply_installation_config(TEST_CONFIG)
        self.original_clock = transit.clock
        self.original_tabela = transit.tabela
        self.original_transit_pred = transit.transit_pred
        transit.clock = ReplayClock()
        transit.replay_time_initialized = False
        transit.metar_t = None
        transit.aktual_t = None
        transit.last_t = None
        transit.gong_t = None
        transit.last_update_time = None
        transit.plane_dict = {}
        transit.tabela = lambda: (0, 0, 0, 0)

    def tearDown(self):
        transit.clock = self.original_clock
        transit.tabela = self.original_tabela
        transit.transit_pred = self.original_transit_pred

    def process(self, generated, logged, port=30106, icao="ABC123"):
        transit.process_line(message(generated, logged, icao), port)

    def test_generated_stays_on_record_and_logged_initializes_clock_and_globals(self):
        generated = "2024/05/18 12:00:00.000"
        logged = "2024/05/18 12:00:00.250"
        self.process(generated, logged)

        generated_utc = utc(generated)
        logged_utc = utc(logged)
        self.assertEqual(transit.plane_dict["ABC123"][0], generated_utc)
        self.assertEqual(transit.clock.now_utc(), logged_utc)
        self.assertEqual(transit.metar_t, logged_utc - datetime.timedelta(seconds=900))
        self.assertEqual(transit.aktual_t, logged_utc)
        self.assertEqual(transit.last_t, logged_utc - datetime.timedelta(seconds=10))
        self.assertEqual(transit.gong_t, logged_utc)
        self.assertEqual(transit.last_update_time, logged_utc)
        self.assertTrue(transit.replay_time_initialized)

    def test_generated_can_move_backward_while_logged_moves_forward(self):
        self.process("2024/05/18 12:00:10.000", "2024/05/18 12:00:10.100")
        self.process("2024/05/18 12:00:09.000", "2024/05/18 12:00:11.100")
        self.assertEqual(transit.plane_dict["ABC123"][0], utc("2024/05/18 12:00:09.000"))
        self.assertEqual(transit.clock.now_utc(), utc("2024/05/18 12:00:11.100"))

    def test_older_and_equal_logged_values_do_not_move_clock_backward(self):
        self.process("2024/05/18 12:00:00.000", "2024/05/18 12:00:10.000")
        self.process("2024/05/18 12:00:01.000", "2024/05/18 12:00:09.000")
        self.process("2024/05/18 12:00:02.000", "2024/05/18 12:00:10.000")
        self.assertEqual(transit.clock.now_utc(), utc("2024/05/18 12:00:10.000"))
        self.assertEqual(transit.plane_dict["ABC123"][0], utc("2024/05/18 12:00:02.000"))

    def test_port_30003_applies_existing_offset_to_both_timestamps(self):
        self.process("2024/05/18 12:00:00.000", "2024/05/18 12:00:01.000", port=30003)
        offset = datetime.timedelta(hours=transit.time.altzone / 60 / 60)
        self.assertEqual(transit.plane_dict["ABC123"][0], utc("2024/05/18 12:00:00.000") + offset)
        self.assertEqual(transit.clock.now_utc(), utc("2024/05/18 12:00:01.000") + offset)

    def test_port_30106_keeps_both_timestamps_as_utc(self):
        self.process("2024/05/18 12:00:00.000", "2024/05/18 12:00:01.000", port=30106)
        self.assertEqual(transit.plane_dict["ABC123"][0], utc("2024/05/18 12:00:00.000"))
        self.assertEqual(transit.clock.now_utc(), utc("2024/05/18 12:00:01.000"))

    def test_historical_record_age_uses_replay_time_and_is_not_removed(self):
        self.process("2024/05/18 12:00:00.000", "2024/05/18 12:00:00.050")
        age = (transit.clock.now_utc() - transit.plane_dict["ABC123"][0]).total_seconds()
        self.assertEqual(age, 0.05)
        self.assertIn("ABC123", transit.plane_dict)

    def test_first_complete_mlat_message_initializes_ephemerides_before_prediction(self):
        historical_time = utc("2024/05/18 12:13:09.187")
        table_times = []
        prediction_states = []

        def historical_table():
            table_times.append(transit.clock.now_utc())
            return 1.0, 2.0, 3.0, 4.0

        def record_prediction(*args):
            prediction_states.append((
                transit.sun_alt,
                transit.sun_az,
                transit.moon_alt,
                transit.moon_az,
                transit.clock.now_utc(),
                args[-2],
                args[-1],
            ))
            return 0

        transit.tabela = historical_table
        transit.transit_pred = record_prediction
        line = "MLAT,3,1,1,48B5CF,1,2024/05/18,12:13:09.187,2024/05/18,12:13:09.187,,1687,97,295,51.7608,21.0416,200,,,,,,,,"

        transit.process_line(line, 30106)

        self.assertEqual(table_times, [historical_time, historical_time])
        self.assertEqual(
            prediction_states,
            [
                (1.0, 2.0, 3.0, 4.0, historical_time, 3.0, 4.0),
                (1.0, 2.0, 3.0, 4.0, historical_time, 1.0, 2.0),
            ],
        )
        self.assertEqual(transit.plane_dict["48B5CF"][0], historical_time)
        self.assertEqual(transit.plane_dict["48B5CF"][2:4], [51.7608, 21.0416])


class RealClockIntegrationTests(unittest.TestCase):
    def test_default_clock_remains_real_and_replay_update_is_noop(self):
        self.assertIsInstance(clock_from_args([]), RealClock)
        original_clock = transit.clock
        try:
            transit.clock = RealClock()
            before = transit.clock.now_utc()
            transit.advance_replay_time(utc("2024/05/18 12:00:00.000"))
            after = transit.clock.now_utc()
            self.assertGreaterEqual(after, before)
            self.assertLess((after - before).total_seconds(), 1)
        finally:
            transit.clock = original_clock


if __name__ == "__main__":
    unittest.main()
