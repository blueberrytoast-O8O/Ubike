import copy
import json
import tempfile
import unittest
from pathlib import Path

from backend.history_adapter import load_history_records, build_station_id_mapping_from_normalized


class HistoryAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "history.csv").write_text(
            "datetime,station_name,total_slots,bikes,district,lng,lat\n"
            "2026-01-01 00:00:00,A,10,3,Alpha,121.1,25.1\n"
            "2026-01-01 00:30:00,A,10,4,Alpha,121.1,25.1\n"
            "2026-01-01 01:00:00,A,10,11,Alpha,121.1,25.1\n"
            "2026-01-01 01:30:00,A,10,-1,Alpha,121.1,25.1\n"
            "2026-01-01 03:00:00,A,10,5,Alpha,121.1,25.1\n"
            "2026-01-01 00:00:00,B,20,7,Beta,121.2,25.2\n",
            encoding="utf-8",
        )
        self.mapping = {"A": "ID-A"}

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, **kwargs):
        return load_history_records(self.dir, self.mapping, **kwargs)

    def test_datetime_localized_taipei(self):
        self.assertTrue(self.load()["records"][0]["observed_at"].endswith("+08:00"))

    def test_model_time_slot(self):
        self.assertEqual(self.load()["records"][0]["model_time_slot"], "2026-01-01T00:00:00+08:00")

    def test_bikes_maps_to_available_bikes(self):
        self.assertEqual(self.load()["records"][0]["available_bikes"], 3)

    def test_station_name_preserved(self):
        self.assertEqual(self.load()["records"][0]["station_name"], "A")

    def test_mapping_supplies_official_station_id(self):
        self.assertEqual(self.load()["records"][0]["official_station_id"], "ID-A")

    def test_missing_mapping_not_fabricated(self):
        result = self.load()
        self.assertFalse(any(row["station_name"] == "B" for row in result["records"]))
        self.assertIn("missing_official_station_id_mapping", [d["code"] for d in result["diagnostics"]])

    def test_bikes_exceed_total_diagnosed(self):
        self.assertTrue(any("bikes > total_slots" in d["message"] for d in self.load()["diagnostics"]))

    def test_negative_bikes_diagnosed(self):
        self.assertTrue(any("bikes < 0" in d["message"] for d in self.load()["diagnostics"]))

    def test_non_30_minute_gap_diagnosed(self):
        self.assertIn("non_30_minute_gap", [d["code"] for d in self.load()["diagnostics"]])

    def test_input_mapping_not_modified(self):
        original = copy.deepcopy(self.mapping)
        self.load()
        self.assertEqual(self.mapping, original)

    def test_deterministic(self):
        self.assertEqual(self.load(), self.load())

    def test_date_and_station_filters(self):
        result = self.load(start="2026-01-01 00:30:00", end="2026-01-01 03:00:00", station_names=["A"])
        self.assertEqual([r["model_time_slot"] for r in result["records"]],
                         ["2026-01-01T00:30:00+08:00", "2026-01-01T03:00:00+08:00"])

    def test_strict_serialization(self):
        json.dumps(self.load(), allow_nan=False)

    def test_build_mapping_from_normalized(self):
        result = build_station_id_mapping_from_normalized([{"station_name": "A", "official_station_id": "ID-A"}])
        self.assertEqual(result["mapping"], {"A": "ID-A"})


if __name__ == "__main__":
    unittest.main()
