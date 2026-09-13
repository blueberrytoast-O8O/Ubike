import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from backend.realtime_normalizer import normalize_snapshots


FIXTURES = Path(__file__).parent / "fixtures"


class RealtimeNormalizerTests(unittest.TestCase):
    def test_normal_fields_and_distinct_identity(self):
        result = normalize_snapshots(FIXTURES / "snapshot_2200.csv")
        row = result["stations"][0]
        self.assertEqual(row["official_station_id"], "001")
        self.assertEqual(row["station_name"], "測試甲站")
        self.assertEqual(row["district"], "八里區")
        self.assertEqual((row["total_slots"], row["available_bikes"], row["available_docks"]), (20, 5, 15))
        self.assertEqual(row["observed_at"], "2026-09-12T22:00:09+08:00")
        self.assertEqual(row["observed_at_source"], "抓取時間")
        self.assertEqual(row["timezone"], "Asia/Taipei")
        self.assertIsNone(row["latitude"])
        self.assertIsNone(row["longitude"])
        self.assertEqual(row["source_file"], str((FIXTURES / "snapshot_2200.csv").resolve()))

    def test_latest_snapshot(self):
        result = normalize_snapshots([FIXTURES / "snapshot_2230.csv", FIXTURES / "snapshot_2200.csv"])
        self.assertEqual(result["latest_observed_at"], "2026-09-12T22:30:29+08:00")
        self.assertEqual(result["stations"][0]["available_bikes"], 6)

    def test_content_not_filename_and_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "20990101_2359.csv").write_bytes((FIXTURES / "snapshot_2200.csv").read_bytes())
            newer = root / "20000101_0000.csv"
            newer.write_bytes((FIXTURES / "snapshot_2230.csv").read_bytes())
            result = normalize_snapshots(root)
            self.assertEqual(result["selected_source_file"], str(newer.resolve()))

    def test_invalid_station_isolation_and_duplicates(self):
        result = normalize_snapshots(FIXTURES / "station_errors.csv")
        self.assertEqual([r["official_station_id"] for r in result["stations"]], ["001", "009"])
        self.assertEqual(result["stations"][0]["station_name"], "LastTie")
        self.assertEqual(result["stations"][0]["available_bikes"], 8)
        codes = [d["code"] for d in result["diagnostics"]]
        self.assertEqual(codes.count("invalid_station"), 6)
        self.assertEqual(codes.count("duplicate_station"), 3)
        self.assertEqual(codes.count("invalid_time"), 1)
        self.assertEqual(codes.count("invalid_coordinate"), 2)
        self.assertIsNone(result["stations"][1]["latitude"])

    def test_reported_docks_preserved_and_checked(self):
        result = normalize_snapshots(FIXTURES / "snapshot_2230.csv")
        self.assertEqual(result["stations"][1]["available_docks"], 7)
        self.assertEqual(result["stations"][1]["available_docks_source"], "reported")
        self.assertIn("dock_count_mismatch", [d["code"] for d in result["diagnostics"]])

    def test_derived_docks_and_utc_conversion(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "derived.csv"
            with (FIXTURES / "station_errors.csv").open(encoding="utf-8") as source:
                row = next(csv.DictReader(source))
            del row["available_docks"]
            with path.open("w", encoding="utf-8-sig", newline="") as target:
                writer = csv.DictWriter(target, fieldnames=list(row))
                writer.writeheader()
                writer.writerow(row)
            result = normalize_snapshots(path)
            station = result["stations"][0]
            self.assertEqual(station["available_docks"], 15)
            self.assertEqual(station["available_docks_source"], "derived_total_minus_bikes")
            self.assertEqual(station["observed_at"], "2026-09-12T22:30:29+08:00")
            self.assertEqual(station["latitude"], 25.1)

    def test_json_serializable_strict(self):
        for path in FIXTURES.glob("*.csv"):
            result = normalize_snapshots(path)
            self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)

    def test_source_bytes_unchanged(self):
        paths = list(FIXTURES.glob("*.csv"))
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        normalize_snapshots(paths)
        for path in paths:
            normalize_snapshots(path)
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})

    def test_missing_file_and_empty_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(normalize_snapshots(folder)["selected_source_file"])
            result = normalize_snapshots(Path(folder) / "missing.csv")
            self.assertEqual(result["stations"], [])
            self.assertIn("unreadable_csv", [d["code"] for d in result["diagnostics"]])


if __name__ == "__main__":
    unittest.main()
