import datetime
import unittest
from unittest.mock import patch

import pytz

import transit_warning as transit
from config import InstallationConfig
from transit_clock import ReplayClock


TEST_CONFIG = InstallationConfig(
    observer_lat=51.0,
    observer_lon=21.0,
    observer_elevation_m=200.0,
    transition_altitude_ft=6500,
    adsb_host="adsb.example",
    adsb_port=31003,
    adsb_timestamp_timezone="Europe/Warsaw",
    mlat_host="mlat.example",
    mlat_port=31106,
    metar_station="EPRA",
)


def utc(value):
    return datetime.datetime.strptime(
        value, "%Y/%m/%d %H:%M:%S.%f").replace(tzinfo=pytz.utc)


def line(prefix, subtype, timestamp, icao="ABC123", altitude="",
         groundspeed="", track="", latitude="", longitude="",
         vertical_rate=""):
    date, timestamp_time = timestamp.split()
    fields = [
        prefix, str(subtype), "1", "1", icao, "1",
        date, timestamp_time, date, timestamp_time, "",
        str(altitude), str(groundspeed), str(track),
        str(latitude), str(longitude), str(vertical_rate),
    ]
    return ",".join(fields)


class AircraftMotionStateTests(unittest.TestCase):
    def setUp(self):
        self.original_clock = transit.clock
        self.original_plane_dict = transit.plane_dict
        self.original_altitude_sources = transit.altitude_sources
        self.original_motion_states = transit.aircraft_motion_states
        self.original_pressure = transit.pressure
        self.original_tabela = transit.tabela
        self.original_transit_pred = transit.transit_pred

        transit.clock = ReplayClock()
        transit.apply_installation_config(TEST_CONFIG)
        transit.replay_time_initialized = False
        transit.plane_dict = {}
        transit.altitude_sources = {}
        transit.aircraft_motion_states = {}
        transit.pressure = 1013.25
        transit.tabela = lambda: (30.0, 120.0, 20.0, 90.0)
        transit.transit_pred = lambda *args: 0

    def tearDown(self):
        transit.clock = self.original_clock
        transit.plane_dict = self.original_plane_dict
        transit.altitude_sources = self.original_altitude_sources
        transit.aircraft_motion_states = self.original_motion_states
        transit.pressure = self.original_pressure
        transit.tabela = self.original_tabela
        transit.transit_pred = self.original_transit_pred

    def process(self, value, port=31003):
        transit.process_line(value, port)

    def test_position_updates_only_from_a_valid_position_message(self):
        first = "2026/08/17 12:00:00.000"
        later = "2026/08/17 12:00:05.000"
        self.process(line("MSG", 3, first, latitude=51.2, longitude=21.3))
        position = transit.get_aircraft_motion_state("ABC123").position

        self.process(line("MSG", 1, later))
        unchanged = transit.get_aircraft_motion_state("ABC123").position

        self.assertEqual((position.latitude, position.longitude), (51.2, 21.3))
        self.assertEqual(position.updated_at_utc, utc("2026/08/17 10:00:00.000"))
        self.assertIs(unchanged, position)

    def test_msg_without_track_or_groundspeed_does_not_refresh_them(self):
        first = "2026/08/17 12:00:00.000"
        later = "2026/08/17 12:00:04.000"
        self.process(line(
            "MSG", 4, first, groundspeed=450, track=91,
            vertical_rate=640))
        original = transit.get_aircraft_motion_state("ABC123")

        self.process(line(
            "MSG", 3, later, altitude=10000,
            latitude=51.2, longitude=21.3))
        current = transit.get_aircraft_motion_state("ABC123")

        self.assertIs(current.track, original.track)
        self.assertIs(current.groundspeed, original.groundspeed)
        self.assertEqual(current.track.updated_at_utc, utc(
            "2026/08/17 10:00:00.000"))
        self.assertEqual(current.groundspeed.value, round(450 * 1.852))

    def test_altitude_timestamp_requires_numeric_altitude(self):
        first = "2026/08/17 12:00:00.000"
        later = "2026/08/17 12:00:03.000"
        self.process(line("MSG", 5, first, altitude=10000))
        original = transit.get_aircraft_motion_state("ABC123").altitude

        self.process(line(
            "MSG", 3, later, altitude="",
            latitude=51.3, longitude=21.4))
        current = transit.get_aircraft_motion_state("ABC123")

        self.assertIs(current.altitude, original)
        self.assertEqual(current.position.updated_at_utc, utc(
            "2026/08/17 10:00:03.000"))

    def test_adsb_vertical_rate_has_value_timestamp_and_source(self):
        timestamp = "2026/08/17 12:00:00.125"
        self.process(line(
            "MSG", 4, timestamp, groundspeed=420, track=180,
            vertical_rate=-1280))

        state = transit.get_aircraft_motion_state("ABC123")
        self.assertEqual(state.vertical_rate.value, -1280.0)
        self.assertEqual(state.vertical_rate.source, "adsb")
        self.assertEqual(state.vertical_rate.updated_at_utc, utc(
            "2026/08/17 10:00:00.125"))

    def test_mlat_msg3_updates_all_supplied_motion_parameters(self):
        timestamp = "2026/08/17 10:00:00.250"
        self.process(line(
            "MLAT", 3, timestamp, altitude=8000, groundspeed=210,
            track=270, latitude=51.4, longitude=21.5,
            vertical_rate=512), 31106)

        state = transit.get_aircraft_motion_state("ABC123")
        expected_time = utc(timestamp)
        self.assertEqual(state.position.source, "mlat")
        self.assertEqual(state.altitude.source, "mlat")
        self.assertEqual(state.track.source, "mlat")
        self.assertEqual(state.groundspeed.source, "mlat")
        self.assertEqual(state.vertical_rate.source, "mlat")
        self.assertTrue(all(
            parameter.updated_at_utc == expected_time
            for parameter in (
                state.position, state.altitude, state.track,
                state.groundspeed, state.vertical_rate)))

    def test_mixed_sources_only_replace_parameters_they_supply(self):
        self.process(line(
            "MSG", 4, "2026/08/17 12:00:00.000",
            groundspeed=430, track=95, vertical_rate=0))
        adsb = transit.get_aircraft_motion_state("ABC123")

        self.process(line(
            "MLAT", 3, "2026/08/17 10:00:02.000",
            altitude=9000, latitude=51.5, longitude=21.6), 31106)
        mixed = transit.get_aircraft_motion_state("ABC123")

        self.assertEqual(mixed.position.source, "mlat")
        self.assertEqual(mixed.altitude.source, "mlat")
        self.assertIs(mixed.track, adsb.track)
        self.assertIs(mixed.groundspeed, adsb.groundspeed)
        self.assertIs(mixed.vertical_rate, adsb.vertical_rate)

    def test_freshness_is_relative_to_explicit_utc_time(self):
        self.process(line(
            "MSG", 4, "2026/08/17 12:00:00.000",
            groundspeed=400, track=45, vertical_rate=256))
        self.process(line(
            "MSG", 3, "2026/08/17 12:00:02.000", altitude=10000,
            latitude=51.2, longitude=21.2))

        freshness = transit.get_aircraft_motion_freshness(
            "ABC123", utc("2026/08/17 10:00:10.000"))
        self.assertEqual(freshness.position_age, 8.0)
        self.assertEqual(freshness.altitude_age, 8.0)
        self.assertEqual(freshness.track_age, 10.0)
        self.assertEqual(freshness.groundspeed_age, 10.0)
        self.assertEqual(freshness.vertical_rate_age, 10.0)

    def test_replay_uses_generated_utc_for_each_parameter(self):
        self.process(line(
            "MSG", 4, "2026/08/17 12:00:00.000",
            groundspeed=400, track=45, vertical_rate=256))
        self.process(line(
            "MSG", 3, "2026/08/17 12:00:03.000", altitude=10000,
            latitude=51.2, longitude=21.2))

        state = transit.get_aircraft_motion_state("ABC123")
        self.assertEqual(state.track.updated_at_utc, utc(
            "2026/08/17 10:00:00.000"))
        self.assertEqual(state.position.updated_at_utc, utc(
            "2026/08/17 10:00:03.000"))
        self.assertEqual(transit.clock.now_utc(), utc(
            "2026/08/17 10:00:03.000"))

    def test_transit_pred_contract_is_unchanged(self):
        original = self.original_transit_pred
        with patch.object(transit, "clock") as controlled_clock:
            controlled_clock.now_utc.return_value = utc(
                "2026/08/17 10:00:00.000")
            before = original(
                (51.0, 21.0), (51.2, 21.2), 45, 800, 10000,
                30, 120)
            after = original(
                (51.0, 21.0), (51.2, 21.2), 45, 800, 10000,
                30, 120)
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
