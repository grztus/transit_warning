import unittest

import transit_warning as transit
from config import InstallationConfig


TRANSIT_ARGS = (
    (51.1111, 21.1111),
    (50.30602, 22.24717),
    192.0,
    10066.02,
    25.9,
    150.5,
)

TEST_CONFIG = InstallationConfig(
    observer_lat=51.1111,
    observer_lon=21.1111,
    observer_elevation_m=111.0,
    adsb_host="127.0.0.1",
    adsb_port=30003,
    mlat_host="127.0.0.1",
    mlat_port=30106,
    metar_station="EPRA",
)


class TransitVelocityTests(unittest.TestCase):
    def setUp(self):
        transit.apply_installation_config(TEST_CONFIG)

    def predict(self, velocity):
        observer, plane, track, elevation, body_alt, body_az = TRANSIT_ARGS
        return transit.transit_pred(observer, plane, track, velocity, elevation, body_alt, body_az)

    def test_zero_velocity_has_no_prediction(self):
        self.assertEqual(self.predict(0), 0)

    def test_negative_velocity_has_no_prediction(self):
        self.assertEqual(self.predict(-1), 0)

    def test_positive_velocity_keeps_prediction(self):
        result = self.predict(100)
        self.assertTrue(result)
        self.assertEqual(len(result), 11)
        self.assertAlmostEqual(result[6], (result[5] / 100) * 3600)


if __name__ == "__main__":
    unittest.main()
