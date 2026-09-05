import csv
import datetime
import io
from pathlib import Path
import tempfile
import unittest

from dashboard_history import CSV_FIELDS, DashboardHistoryStore


UTC = datetime.timezone.utc


def record(index, date="2026-08-31", body="SUN", callsign=None,
           outcome="PASSED"):
    event_time = "{}T12:{:02d}:{:02d}.123456Z".format(
        date, (index // 60) % 60, index % 60)
    return {
        "event_id": "ID{:04d}".format(index),
        "body": body,
        "icao": "A{:05d}".format(index),
        "callsign": callsign,
        "predicted_event_utc": event_time,
        "outcome": outcome,
        "first_separation_deg": 2.0,
        "minimum_separation_deg": 1.0,
        "final_separation_deg": 1.5,
        "first_seen_utc": event_time,
        "last_seen_utc": event_time,
        "history_recorded_at_utc": event_time,
        "body_azimuth_deg": 120.0,
        "body_elevation_deg": 20.0,
        "aircraft_elevation_deg": 21.5,
        "distance_km": 100.0,
        "transit_distance_km": 34.4,
        "telegram_range": True,
    }


class DashboardHistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "history"
        self.errors = []
        self.store = DashboardHistoryStore(self.directory, self.errors.append)

    def tearDown(self):
        self.temp.cleanup()

    def test_persists_across_restart_and_partitions_by_event_utc_date(self):
        self.assertTrue(self.store.append(record(1, "2026-08-30")))
        self.assertTrue(self.store.append(record(2, "2026-08-31")))
        restarted = DashboardHistoryStore(self.directory)
        self.assertEqual(["ID0002", "ID0001"], [item["event_id"] for item in
            restarted.query(limit=10)["records"]])
        self.assertTrue((self.directory / "2026-08-30.jsonl").exists())
        self.assertTrue((self.directory / "2026-08-31.jsonl").exists())

    def test_more_than_one_hundred_records_are_retrievable_newest_first(self):
        for index in range(125):
            self.store.append(record(index))
        first = self.store.query(limit=100)
        second = self.store.query(offset=first["next_offset"], limit=100)
        combined = first["records"] + second["records"]
        self.assertEqual(125, len(combined))
        self.assertEqual("ID0124", combined[0]["event_id"])
        self.assertEqual("ID0000", combined[-1]["event_id"])

    def test_rows_are_sorted_by_displayed_event_utc_not_append_order(self):
        for index in (30, 50, 10, 40, 20):
            self.store.append(record(index))
        rows = self.store.query(limit=10)["records"]
        displayed = [item["predicted_event_utc"] for item in rows]
        self.assertEqual(sorted(displayed, reverse=True), displayed)
        self.assertEqual(
            ["ID0050", "ID0040", "ID0030", "ID0020", "ID0010"],
            [item["event_id"] for item in rows])

    def test_date_callsign_body_and_combined_filters(self):
        self.store.append(record(1, "2026-08-30", "SUN", "Alpha123"))
        self.store.append(record(2, "2026-08-31", "MOON", "beta456"))
        self.store.append(record(3, "2026-08-31", "SUN", None))
        self.assertEqual(1, len(self.store.query(
            utc_date="2026-08-30", limit=10)["records"]))
        self.assertEqual("ID0001", self.store.query(
            callsign="PHA1", limit=10)["records"][0]["event_id"])
        self.assertEqual(["ID0002"], [item["event_id"] for item in
            self.store.query(body="MOON", limit=10)["records"]])
        self.assertEqual("ID0002", self.store.query(
            utc_date="2026-08-31", callsign="BETA", body="MOON",
            limit=10)["records"][0]["event_id"])
        self.assertEqual([], self.store.query(
            callsign="missing", limit=10)["records"])

    def test_pagination_is_bounded_and_reports_load_more(self):
        for index in range(30):
            self.store.append(record(index))
        page = self.store.query(limit=10)
        self.assertEqual(10, len(page["records"]))
        self.assertTrue(page["has_more"])
        self.assertEqual(10, page["next_offset"])
        final = self.store.query(offset=20, limit=10)
        self.assertFalse(final["has_more"])
        self.assertIsNone(final["next_offset"])

    def test_max_sep_filter_is_inclusive_and_absent_is_unchanged(self):
        boundary = record(1)
        boundary["final_separation_deg"] = 3.0
        above = record(2)
        above["final_separation_deg"] = 3.000001
        missing = record(3)
        missing.pop("final_separation_deg")
        invalid = record(4)
        invalid["final_separation_deg"] = "invalid"
        for value in (boundary, above, missing, invalid):
            self.store.append(value)
        self.assertEqual(4, len(self.store.query(limit=10)["records"]))
        self.assertEqual(["ID0001"], [item["event_id"] for item in
            self.store.query(limit=10, max_sep_deg=3.0)["records"]])

    def test_max_sep_combines_with_filters_before_pagination(self):
        for index in range(6):
            value = record(index, date="2026-08-31",
                           body="MOON" if index % 2 else "SUN",
                           callsign="KEEP" if index >= 2 else "DROP")
            value["final_separation_deg"] = float(index)
            self.store.append(value)
        first = self.store.query(
            utc_date="2026-08-31", callsign="keep", body="MOON",
            max_sep_deg=5.0, limit=1)
        second = self.store.query(
            utc_date="2026-08-31", callsign="keep", body="MOON",
            max_sep_deg=5.0, offset=first["next_offset"], limit=1)
        self.assertEqual(["ID0005"], [x["event_id"] for x in first["records"]])
        self.assertEqual(["ID0003"], [x["event_id"] for x in second["records"]])
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])

    def test_csv_quotes_values_and_empty_export_has_header(self):
        special = record(1, callsign='A,"B"')
        self.store.append(special)
        rows = list(csv.DictReader(io.StringIO(
            self.store.export_csv().decode("utf-8-sig"))))
        self.assertEqual('A,"B"', rows[0]["callsign"])
        self.assertEqual(list(CSV_FIELDS), list(rows[0]))
        self.assertEqual("34.4", rows[0]["transit_distance_km"])
        empty = DashboardHistoryStore(Path(self.temp.name) / "empty")
        self.assertEqual(1, len(empty.export_csv().decode(
            "utf-8-sig").splitlines()))

    def test_filtered_export_is_not_limited_to_one_hundred(self):
        for index in range(115):
            self.store.append(record(index, callsign="KEEP"))
        self.store.append(record(200, callsign="DROP"))
        rows = list(csv.DictReader(io.StringIO(self.store.export_csv(
            callsign="keep").decode("utf-8-sig"))))
        self.assertEqual(115, len(rows))

    def test_duplicate_event_is_not_appended_twice(self):
        value = record(1)
        self.assertTrue(self.store.append(value))
        self.assertFalse(self.store.append(value))
        self.assertEqual(1, len(self.store.query(limit=10)["records"]))

    def test_corrupt_truncated_line_does_not_hide_valid_records(self):
        self.store.append(record(1))
        path = self.directory / "2026-08-31.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            stream.write('{"event_id":"truncated"')
        self.assertEqual("ID0001", self.store.query(
            limit=10)["records"][0]["event_id"])

    def test_write_failure_is_fail_open_and_reported_once(self):
        blocked = Path(self.temp.name) / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        store = DashboardHistoryStore(blocked, self.errors.append)
        self.assertFalse(store.append(record(1)))
        self.assertFalse(store.append(record(2)))
        self.assertTrue(store.failed)
        self.assertEqual(1, len(self.errors))

    def test_empty_and_legacy_directory_start_cleanly(self):
        self.assertEqual([], self.store.query()["records"])
        self.directory.mkdir(parents=True)
        (self.directory / "unrelated.txt").write_text("legacy", encoding="utf-8")
        self.assertEqual([], self.store.query()["records"])


if __name__ == "__main__":
    unittest.main()
