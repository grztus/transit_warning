import unittest

from transit_warning import transit_pred


TRANSIT_ARGS = (
    (51.1111, 21.1111),
    (50.30602, 22.24717),
    192.0,
    10066.02,
    25.9,
    150.5,
)


class TransitVelocityTests(unittest.TestCase):
    def predict(self, velocity):
        observer, plane, track, elevation, body_alt, body_az = TRANSIT_ARGS
        return transit_pred(observer, plane, track, velocity, elevation, body_alt, body_az)

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
