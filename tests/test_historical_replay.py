import json
import os
import io
import zipfile
import tempfile
import unittest
from pathlib import Path

from backend.feature_engineering import FEATURES, build_features
from backend.history_adapter import load_history_records
from backend.model_service import ModelService

TAIPEI = "+08:00"


def make_rows(station="A", sid="ID-A", total=20, start="2026-05-01T00:00:00+08:00", count=675):
    import pandas as pd
    base = pd.Timestamp(start)
    return [dict(official_station_id=sid, station_name=station, district="D",
                 observed_at=(base + pd.Timedelta(minutes=30*i)).isoformat(),
                 model_time_slot=(base + pd.Timedelta(minutes=30*i)).isoformat(),
                 total_slots=total, available_bikes=i % total,
                 latitude=25.0, longitude=121.0)
            for i in range(count)]


class HistoricalReplayContractTests(unittest.TestCase):
    def setUp(self):
        self.categories = ["A"]
        self.rows = make_rows()
        self.current = [self.rows[672]]
        self.history = self.rows[:672]
        self.actual_30 = self.rows[673]
        self.actual_60 = self.rows[674]

    def test_no_future_leakage(self):
        base = build_features(self.current, self.history, self.categories,
                              holiday_calendar_path=Path(__file__).parents[1] / "backend/config/training_holidays.json")
        leaked = build_features(self.current, self.history + [self.actual_30, self.actual_60], self.categories,
                                holiday_calendar_path=Path(__file__).parents[1] / "backend/config/training_holidays.json")
        self.assertEqual(base["features"].to_dict("list"), leaked["features"].to_dict("list"))
        self.assertTrue(any(d["code"] == "future_history_ignored" for d in leaked["diagnostics"]))

    def test_30min_actual_is_t_plus_1(self):
        self.assertEqual(self.actual_30["model_time_slot"], "2026-05-15T00:30:00+08:00")

    def test_60min_actual_is_t_plus_2(self):
        self.assertEqual(self.actual_60["model_time_slot"], "2026-05-15T01:00:00+08:00")

    def test_model_feature_order_matches_pack(self):
        path = os.environ.get("YOUBIKE_REAL_MODEL_TEST_PATH",
                              r"C:\Users\gkjk0\Downloads\Ubike-main\Ubike-main\ntpc_youbike_optimized_pack.zip")
        service = ModelService(path)
        loaded = service.load()
        self.assertEqual(loaded["status"], "ready")
        joblib = service._imports()["joblib"]
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                names = [n for n in archive.namelist() if n.lower().endswith(".pkl")]
                pack = joblib.load(io.BytesIO(archive.read(names[0])))
        else:
            pack = joblib.load(path)
        self.assertEqual(tuple(pack["features"]), FEATURES)

    def test_actual_rows_not_in_prediction_features(self):
        result = build_features(self.current, self.history, self.categories,
                                holiday_calendar_path=Path(__file__).parents[1] / "backend/config/training_holidays.json")
        features = result["features"].iloc[0]
        self.assertEqual(features["bikes"], self.current[0]["available_bikes"])
        self.assertNotEqual(features["bikes"], self.actual_30["available_bikes"])

    def test_invalid_history_rows_diagnosed_by_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.csv"
            path.write_text(
                "datetime,station_name,total_slots,bikes,district,lng,lat\n"
                "2026-01-01 00:00:00,A,10,11,D,121,25\n",
                encoding="utf-8",
            )
            result = load_history_records(path, {"A": "ID-A"})
            self.assertEqual(result["records"], [])
            self.assertTrue(any("bikes > total_slots" in d["message"] for d in result["diagnostics"]))

    def test_prediction_and_actual_json_serializable(self):
        payload = dict(station_name="A", prediction_origin=self.current[0]["observed_at"],
                       predicted_bikes_30m=1.25, predicted_bikes_60m=2.5,
                       actual_bikes_30m=self.actual_30["available_bikes"],
                       actual_bikes_60m=self.actual_60["available_bikes"])
        json.dumps(payload, allow_nan=False)

    def test_repeated_replay_deterministic(self):
        a = build_features(self.current, self.history, self.categories,
                           holiday_calendar_path=Path(__file__).parents[1] / "backend/config/training_holidays.json")
        b = build_features(self.current, self.history, self.categories,
                           holiday_calendar_path=Path(__file__).parents[1] / "backend/config/training_holidays.json")
        self.assertEqual(a["features"].to_json(), b["features"].to_json())
        self.assertEqual(a["stations"], b["stations"])


if __name__ == "__main__":
    unittest.main()
