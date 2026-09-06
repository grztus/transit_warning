import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import transit_warning as transit
from shadow_2d_prediction import Shadow2DDiagnosticWriter
from test_motion_freshness import NOW, state


class MotionStaleDiagnosticTests(unittest.TestCase):
    def test_all_required_missing_and_stale_fields_reuse_evaluator_reasons(self):
        for name in ("position", "altitude", "track", "groundspeed"):
            for age, code in ((None, "MISSING_" + name.upper()),
                              (11, name.upper() + "_AGE_GT_10")):
                with self.subTest(name=name, age=age):
                    result = transit.assess_motion_freshness(state(**{name + "_age": age}), NOW)
                    self.assertEqual(transit.MotionFreshnessStatus.STALE, result.status)
                    self.assertIn(code, result.diagnostic["reason_codes"])
                    self.assertEqual(list(result.reason_codes), result.diagnostic["reason_codes"])
                    details = result.diagnostic["fields"][name]
                    self.assertEqual(age, details["age_seconds"])
                    self.assertEqual(None if age is None else "adsb", details["source"])
                    self.assertEqual(None if age is None else "2026-08-19T11:59:49Z", details["updated_at_utc"])

    def test_timestamp_mismatches_and_exact_thresholds(self):
        for name in ("track", "groundspeed"):
            result = transit.assess_motion_freshness(state(**{name + "_age": 11}), NOW)
            self.assertIn("POSITION_" + name.upper() + "_DELTA_GT_10", result.diagnostic["reason_codes"])
            self.assertEqual(11, result.diagnostic["position_" + name + "_delta_seconds"])
        fresh = transit.assess_motion_freshness(state(), NOW)
        self.assertEqual(transit.MotionFreshnessStatus.FRESH, fresh.status)
        self.assertEqual([], fresh.diagnostic["reason_codes"])
        self.assertEqual({"fresh_position": 3, "fresh_parameter": 5, "fresh_delta": 3,
                          "stale_age": 10, "stale_delta": 10}, fresh.diagnostic["thresholds_seconds"])
        self.assertEqual(180, fresh.diagnostic["fields"]["track"]["value"])
        self.assertEqual(800, fresh.diagnostic["fields"]["groundspeed"]["value"])
        self.assertNotIn("value", fresh.diagnostic["fields"]["position"])

    def test_runtime_writer_passes_same_assessment_and_preserves_legacy_fields(self):
        result = transit.assess_motion_freshness(state(track_age=11), NOW)
        with tempfile.TemporaryDirectory() as directory:
            writer = Shadow2DDiagnosticWriter(directory)
            for body in ("SUN", "MOON"):
                writer.record({"icao": "ABC123", "body": body,
                    "utc": (NOW - datetime.timedelta(seconds=2)).isoformat(), "stage": "EXACT"})
            with patch.object(transit, "shadow_2d_diagnostics", writer):
                transit.withdraw_shadow_2d("ABC123", "TEST", NOW, "MOTION_STALE", freshness=result)
            records = [json.loads(line) for line in next(Path(directory).rglob("*.jsonl")).read_text().splitlines()]
            withdrawals = records[-2:]
            for record in withdrawals:
                self.assertEqual("MOTION_STALE", record["reason"])
                self.assertEqual("WITHDRAWN", record["solver_status"])
                self.assertEqual(result.diagnostic, record["motion_freshness"])
                self.assertNotIn("observer", json.dumps(record))
                self.assertNotIn("51.2", json.dumps(record))
            self.assertFalse(writer.withdraw("ABC123", "TEST", "SUN", NOW, "MOTION_STALE"))
