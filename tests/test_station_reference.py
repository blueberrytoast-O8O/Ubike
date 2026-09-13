import copy
import json
import tempfile
import unittest
from pathlib import Path

from backend.station_reference import build_station_reference, enrich_station_coordinates


class StationReferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "coords.csv").write_text(
            "datetime,station_name,total_slots,bikes,district,lng,lat\n"
            "2026-01-01 00:00:00,A,10,3,Alpha,121.1,25.1\n"
            "2026-01-01 00:00:00,B,20,7,Beta,121.2,25.2\n"
            "2026-01-02 00:00:00,B,20,7,Beta,121.3,25.3\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def reference(self):
        return build_station_reference(self.dir)

    def test_single_coordinate_reference(self):
        ref = self.reference()["station_reference"]["A"]
        self.assertEqual((ref["latitude"], ref["longitude"]), (25.1, 121.1))

    def test_multiple_coordinates_latest_observation(self):
        ref = self.reference()["station_reference"]["B"]
        self.assertEqual((ref["latitude"], ref["longitude"]), (25.3, 121.3))

    def test_multiple_coordinates_diagnostic(self):
        diagnostics = self.reference()["diagnostics"]
        self.assertTrue(any(d["code"] == "multiple_historical_coordinates" and d["distinct_coordinate_count"] == 2
                            for d in diagnostics))

    def test_enrich_by_station_name(self):
        rows = [{"official_station_id": "ID-A", "station_name": "A", "available_bikes": 1,
                 "available_docks": 9, "latitude": None, "longitude": None}]
        enriched = enrich_station_coordinates(rows, self.reference())["stations"][0]
        self.assertEqual((enriched["latitude"], enriched["longitude"]), (25.1, 121.1))

    def test_official_station_id_unchanged(self):
        rows = [{"official_station_id": "ID-A", "station_name": "A", "latitude": None, "longitude": None}]
        self.assertEqual(enrich_station_coordinates(rows, self.reference())["stations"][0]["official_station_id"], "ID-A")

    def test_current_counts_unchanged(self):
        rows = [{"official_station_id": "ID-A", "station_name": "A", "available_bikes": 4,
                 "available_docks": 6, "latitude": None, "longitude": None}]
        enriched = enrich_station_coordinates(rows, self.reference())["stations"][0]
        self.assertEqual((enriched["available_bikes"], enriched["available_docks"]), (4, 6))

    def test_existing_coordinates_not_overwritten(self):
        rows = [{"official_station_id": "ID-A", "station_name": "A", "latitude": 24.0, "longitude": 120.0}]
        enriched = enrich_station_coordinates(rows, self.reference())["stations"][0]
        self.assertEqual((enriched["latitude"], enriched["longitude"]), (24.0, 120.0))

    def test_missing_station_reference(self):
        rows = [{"official_station_id": "ID-X", "station_name": "X", "latitude": None, "longitude": None}]
        result = enrich_station_coordinates(rows, self.reference())
        self.assertIsNone(result["stations"][0]["latitude"])
        self.assertEqual(result["diagnostics"][0]["code"], "station_reference_not_found")

    def test_input_not_modified(self):
        rows = [{"official_station_id": "ID-A", "station_name": "A", "latitude": None, "longitude": None}]
        original = copy.deepcopy(rows)
        enrich_station_coordinates(rows, self.reference())
        self.assertEqual(rows, original)

    def test_deterministic(self):
        self.assertEqual(self.reference(), self.reference())

    def test_strict_json_serialization(self):
        json.dumps(self.reference(), allow_nan=False)
        json.dumps(enrich_station_coordinates([{"station_name": "A", "latitude": None, "longitude": None}], self.reference()),
                   allow_nan=False)


if __name__ == "__main__":
    unittest.main()
