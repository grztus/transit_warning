import datetime
import unittest

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
    adsb_timestamp_timezone="UTC",
    mlat_host="mlat.example",
    mlat_port=31106,
    metar_station="EPRA",
)


def utc(value):
    return datetime.datetime.strptime(
        value, "%Y/%m/%d %H:%M:%S.%f").replace(tzinfo=pytz.utc)


def msg3(icao, timestamp, altitude):
    date, timestamp_time = timestamp.split()
    return (
        "MSG,3,1,1,{icao},1,{date},{time},{date},{time},,{altitude},,,"
        "51.2,21.2"
    ).format(icao=icao, date=date, time=timestamp_time, altitude=altitude)


def msg5(icao, timestamp, altitude, prefix="MSG"):
    date, timestamp_time = timestamp.split()
    return (
        "{prefix},5,1,1,{icao},1,{date},{time},{date},{time},TEST123,{altitude}"
    ).format(
        prefix=prefix, icao=icao, date=date, time=timestamp_time,
        altitude=altitude,
    )


class AltitudeProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.original_clock = transit.clock
        self.original_plane_dict = transit.plane_dict
        self.original_altitude_sources = transit.altitude_sources
        self.original_pressure = transit.pressure
        self.original_tabela = transit.tabela
        transit.clock = ReplayClock()
        transit.apply_installation_config(TEST_CONFIG)
        transit.replay_time_initialized = False
        transit.plane_dict = {}
        transit.altitude_sources = {}
        transit.pressure = 1000.5
        transit.tabela = lambda: (0, 0, 0, 0)

    def tearDown(self):
        transit.clock = self.original_clock
        transit.plane_dict = self.original_plane_dict
        transit.altitude_sources = self.original_altitude_sources
        transit.pressure = self.original_pressure
        transit.tabela = self.original_tabela

    def expected_metres(self, altitude_ft):
        return transit.correct_pressure_altitude(altitude_ft, 1000.5) * 0.3048

    def test_adsb_msg3_records_raw_corrected_kind_type_and_generated_time(self):
        timestamp = "2026/08/17 10:00:00.125"
        transit.process_line(msg3("ABC001", timestamp, 35000), 31003)

        measurement = transit.altitude_sources["ABC001"]["adsb"]
        self.assertEqual(measurement.source, "adsb")
        self.assertEqual(measurement.altitude_kind, "barometric")
        self.assertEqual(measurement.altitude_baro_ft, 35000)
        self.assertEqual(
            measurement.altitude_corrected_m, self.expected_metres(35000))
        self.assertEqual(measurement.timestamp_utc, utc(timestamp))
        self.assertEqual(measurement.message_type, "MSG,3")
        self.assertEqual(
            transit.plane_dict["ABC001"][4], measurement.altitude_corrected_m)

    def test_msg5_updates_only_the_matching_source(self):
        transit.process_line(
            msg5("ABC002", "2026/08/17 10:00:00.000", 34975, "MLAT"),
            31106,
        )
        original_mlat = transit.altitude_sources["ABC002"]["mlat"]
        transit.process_line(
            msg5("ABC002", "2026/08/17 10:00:01.000", 35025), 31003)

        measurements = transit.altitude_sources["ABC002"]
        self.assertIs(measurements["mlat"], original_mlat)
        self.assertEqual(measurements["mlat"].message_type, "MSG,5")
        self.assertEqual(measurements["adsb"].altitude_baro_ft, 35025)
        self.assertEqual(measurements["adsb"].message_type, "MSG,5")

    def test_sources_remain_independent_and_last_message_wins_geometry(self):
        transit.process_line(
            msg3("ABC003", "2026/08/17 10:00:00.000", 35000), 31003)
        original_adsb = transit.altitude_sources["ABC003"]["adsb"]
        transit.process_line(
            msg3("ABC003", "2026/08/17 10:00:01.000", 34975), 31106)
        self.assertIs(transit.altitude_sources["ABC003"]["adsb"], original_adsb)
        original_mlat = transit.altitude_sources["ABC003"]["mlat"]
        transit.process_line(
            msg5("ABC003", "2026/08/17 10:00:02.000", 35025), 31003)
        self.assertIs(transit.altitude_sources["ABC003"]["mlat"], original_mlat)

        diagnostics = transit.get_altitude_diagnostics(
            "ABC003", utc("2026/08/17 10:00:12.000"))
        self.assertEqual(diagnostics.latest_adsb.altitude_baro_ft, 35025)
        self.assertEqual(diagnostics.latest_mlat.altitude_baro_ft, 34975)
        self.assertEqual(
            diagnostics.latest_mlat.altitude_corrected_m,
            self.expected_metres(34975),
        )
        self.assertEqual(diagnostics.latest_mlat.altitude_kind, "barometric")
        self.assertEqual(diagnostics.latest_mlat.message_type, "MSG,3")
        self.assertEqual(
            diagnostics.latest_adsb.timestamp_utc,
            utc("2026/08/17 10:00:02.000"),
        )
        self.assertEqual(
            diagnostics.latest_mlat.timestamp_utc,
            utc("2026/08/17 10:00:01.000"),
        )
        self.assertEqual(diagnostics.delta_adsb_mlat_ft, 50)
        self.assertEqual(diagnostics.adsb_age_seconds, 10.0)
        self.assertEqual(diagnostics.mlat_age_seconds, 11.0)
        self.assertEqual(
            diagnostics.current_geometry_altitude_m,
            self.expected_metres(35025),
        )

    def test_missing_source_is_reported_without_error(self):
        timestamp = "2026/08/17 10:00:00.000"
        transit.process_line(msg3("ABC004", timestamp, 12000), 31106)

        diagnostics = transit.get_altitude_diagnostics(
            "ABC004", utc("2026/08/17 10:00:05.000"))
        self.assertIsNone(diagnostics.latest_adsb)
        self.assertIsNone(diagnostics.delta_adsb_mlat_ft)
        self.assertIsNone(diagnostics.adsb_age_seconds)
        self.assertEqual(diagnostics.latest_mlat.altitude_baro_ft, 12000)
        self.assertEqual(diagnostics.mlat_age_seconds, 5.0)


if __name__ == "__main__":
    unittest.main()
