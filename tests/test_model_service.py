"""Stub tests are isolated from opt-in real joblib tests.

Set YOUBIKE_REAL_MODEL_TEST_PATH to a trusted local pkl/ZIP to enable real tests.
Synthetic history proves inference mechanics only, not forecasting accuracy.
"""
import importlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

import numpy as np
import pandas as pd

from backend.feature_engineering import FEATURES, build_features
from backend.model_service import ModelService
from test_feature_engineering import sequence


class ModelServiceStubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "stub.pkl"
        self.path.write_bytes(b"stub; joblib.load is patched")
        self.rows = sequence()
        self.batch = build_features([self.rows[-1]], self.rows[:-1], ["Alpha", "Beta"])
        self.models = {}
        for key, value in [("30min", -2.75), ("60min", 120.25)]:
            self.models[key] = SimpleNamespace(feature_name_=list(FEATURES), best_iteration_=10,
                booster_=SimpleNamespace(pandas_categorical=[["Alpha", "Beta"]]),
                predict=Mock(return_value=np.array([value])))
        self.pack = dict(models=self.models, features=list(FEATURES), stations=["Alpha", "Beta"],
                         sampling_interval="30min", target_type="absolute")
        self.loader = self.enterContext(patch("joblib.load", return_value=self.pack))
        self.service = ModelService(self.path)

    def infer(self, frame=None, stations=None):
        return self.service.predict_features(self.batch["features"] if frame is None else frame,
                                            self.batch["stations"] if stations is None else stations)

    def assertBlocked(self, result, code):
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["predictions"], [])
        self.assertIn(code, [d["code"] for d in result["diagnostics"]])
        json.dumps(result, allow_nan=False)

    def test_missing_path(self):
        self.assertBlocked(ModelService(self.path.with_name("missing.pkl")).load(), "model_path_not_found")
        self.loader.assert_not_called()

    def test_missing_dependency_distinguished(self):
        real_import = importlib.import_module
        def missing(name):
            if name == "lightgbm":
                raise ModuleNotFoundError("No module named 'lightgbm'")
            return real_import(name)
        with patch("backend.model_service.importlib.import_module", side_effect=missing):
            self.assertBlocked(self.service.load(), "dependency_missing")
        self.loader.assert_not_called()

    def test_pack_missing_either_horizon(self):
        for key in ("30min", "60min"):
            with self.subTest(key=key):
                pack = dict(self.pack, models={k: v for k, v in self.models.items() if k != key})
                self.loader.return_value = pack
                self.assertBlocked(ModelService(self.path).load(), "missing_horizon_model")

    def test_missing_extra_and_reordered_features(self):
        frame = self.batch["features"]
        for invalid in (frame.drop(columns="bikes"), frame.assign(extra=0), frame[list(reversed(FEATURES))]):
            self.assertBlocked(self.infer(invalid), "feature_schema_mismatch")
        for model in self.models.values():
            model.predict.assert_not_called()

    def test_wrong_dtype_rejected(self):
        frame = self.batch["features"].astype({"bikes": "float64"})
        self.assertBlocked(self.infer(frame), "feature_dtype_mismatch")

    def test_unknown_station_not_encoded(self):
        self.rows[-1]["station_name"] = "Unknown"
        self.assertBlocked(self.service.predict([self.rows[-1]], self.rows[:-1]), "unknown_model_station")
        frame = self.batch["features"].copy()
        frame["station_id"] = pd.Categorical(["Unknown"], categories=["Alpha", "Beta"])
        self.assertBlocked(self.infer(frame), "unknown_model_station")

    def test_category_order_and_ordered_flag_rejected(self):
        for categories, ordered in [(["Beta", "Alpha"], False), (["Alpha", "Beta"], True)]:
            frame = self.batch["features"].copy()
            frame["station_id"] = pd.Categorical(["Alpha"], categories=categories, ordered=ordered)
            self.assertBlocked(self.infer(frame), "model_category_mismatch")

    def test_both_models_called_separately(self):
        result = self.infer()
        self.assertEqual(result["status"], "ready")
        self.assertEqual([r["raw_prediction"] for r in result["predictions"]], [-2.75, 120.25])
        for model in self.models.values():
            model.predict.assert_called_once()
            self.assertEqual(model.predict.call_args.kwargs, {"num_iteration": 10})

    def test_prediction_times_cross_midnight(self):
        rows = sequence(end="2026-05-20T23:30:00+08:00")
        result = self.service.predict([rows[-1]], rows[:-1])
        self.assertEqual([r["predicted_at"] for r in result["predictions"]],
                         ["2026-05-21T00:00:00+08:00", "2026-05-21T00:30:00+08:00"])
        self.assertEqual([r["horizon_minutes"] for r in result["predictions"]], [30, 60])

    def test_clipping_preserves_raw(self):
        rows = self.infer()["predictions"]
        self.assertEqual([r["predicted_bikes"] for r in rows], [0, 100])
        self.assertEqual([r["raw_prediction"] for r in rows], [-2.75, 120.25])

    def test_half_up_rounding(self):
        self.models["30min"].predict.return_value = np.array([2.5])
        self.models["60min"].predict.return_value = np.array([2.49])
        self.assertEqual([r["predicted_bikes"] for r in self.infer()["predictions"]], [3, 2])

    def test_strict_json(self):
        result = self.infer()
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)
        row = result["predictions"][0]
        self.assertTrue({"official_station_id", "station_name", "observed_at", "predicted_at", "horizon_minutes", "raw_prediction", "predicted_bikes", "total_slots", "model_mode", "diagnostics"} <= row.keys())

    def test_real_failure_never_falls_back_to_mock(self):
        self.models["60min"].predict.side_effect = RuntimeError("real model failed")
        result = self.infer()
        self.assertBlocked(result, "inference_failed")
        self.assertEqual(result["model_mode"], "real")

    def test_corrupt_load_blocked(self):
        self.loader.side_effect = ValueError("corrupt artifact")
        self.assertBlocked(self.service.load(), "model_load_failed")

    def test_nonfinite_or_wrong_shape_predictions_rejected(self):
        for values in (np.array([np.nan]), np.array([np.inf]), np.array([[2.0]])):
            self.models["30min"].predict.return_value = values
            self.assertBlocked(self.infer(), "invalid_model_prediction")

    def test_nan_feature_rejected_before_predict(self):
        frame = self.batch["features"].copy()
        frame["change_rate_30m"] = np.nan
        self.assertBlocked(self.infer(frame), "undefined_features")
        self.models["30min"].predict.assert_not_called()

    def test_model_category_contract_disagreement(self):
        self.models["60min"].booster_.pandas_categorical = [["Beta", "Alpha"]]
        self.assertBlocked(self.service.load(), "model_category_mismatch")

    def test_lazy_import_and_single_load(self):
        import backend.model_service as module
        importlib.reload(module)
        service = module.ModelService(self.path)
        self.loader.assert_not_called()
        self.assertEqual(service.load()["status"], "ready")
        self.assertEqual(service.load()["status"], "ready")
        self.loader.assert_called_once()

    def test_environment_path(self):
        with patch.dict(os.environ, {"YOUBIKE_MODEL_PATH": str(self.path)}):
            self.assertEqual(ModelService().load()["status"], "ready")

    def test_short_history_blocked_not_fake_prediction(self):
        result = self.service.predict([self.rows[-1]], self.rows[-3:-1])
        self.assertBlocked(result, "insufficient_history")
        for model in self.models.values():
            model.predict.assert_not_called()

    def test_explicit_mock_still_requires_history(self):
        mock = ModelService(mode="mock", mock_categories=["Alpha"])
        result = mock.predict([self.rows[-1]], self.rows[:-1])
        self.assertEqual(result["status"], "ready")
        self.assertTrue(all(r["model_mode"] == "mock" for r in result["predictions"]))
        self.assertEqual(result["predictions"][0]["raw_prediction"], self.rows[-1]["available_bikes"])
        self.assertBlocked(mock.predict([self.rows[-1]], []), "insufficient_history")
        self.loader.assert_not_called()

    def test_metadata_cannot_substitute_official_id_for_name(self):
        stations = [dict(self.batch["stations"][0], station_name="001")]
        self.assertBlocked(self.infer(stations=stations), "unknown_model_station")

    def test_non_json_diagnostics_rejected(self):
        stations = [dict(self.batch["stations"][0], diagnostics=[{"value": float("nan")}])]
        self.assertBlocked(self.infer(stations=stations), "invalid_metadata")

    def test_alignment_setting_passed_to_feature_builder(self):
        self.rows[-1]["observed_at"] = "2026-05-20T12:30:29+08:00"
        self.service.alignment_tolerance_seconds = 20
        self.assertBlocked(self.service.predict([self.rows[-1]], self.rows[:-1]), "off_schedule_snapshot")
        self.service.alignment_tolerance_seconds = 120
        result = self.service.predict([self.rows[-1]], self.rows[:-1])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["predictions"][0]["observed_at"], self.rows[-1]["observed_at"])
        self.assertEqual(result["predictions"][0]["model_time_slot"], "2026-05-20T12:30:00+08:00")
        self.assertEqual(result["predictions"][0]["predicted_at"], "2026-05-20T13:00:29+08:00")

    def test_calendar_setting_passed_to_feature_builder(self):
        calendar = Path(self.temp.name) / "calendar.json"
        calendar.write_text(json.dumps(dict(coverage_start="2026-05-01", coverage_end="2026-05-19", holidays=[])), encoding="utf-8")
        self.service.holiday_calendar_path = calendar
        self.assertBlocked(self.service.predict([self.rows[-1]], self.rows[:-1]), "holiday_calendar_out_of_range")
        self.models["30min"].predict.assert_not_called()


@unittest.skipUnless(os.environ.get("YOUBIKE_REAL_MODEL_TEST_PATH"), "Real pkl tests opt in via YOUBIKE_REAL_MODEL_TEST_PATH")
class RealModelIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = ModelService(os.environ["YOUBIKE_REAL_MODEL_TEST_PATH"])
        cls.loaded = cls.service.load()

    def test_real_joblib_load_and_categories(self):
        self.assertEqual(self.loaded["status"], "ready", self.loaded)
        self.assertGreater(len(self.loaded["categories"]), 0)

    def test_real_both_horizons_with_synthetic_history(self):
        self.assertEqual(self.loaded["status"], "ready", self.loaded)
        rows = sequence(name=self.loaded["categories"][0], sid="synthetic-official-id")
        result = self.service.predict([rows[-1]], rows[:-1])
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual([r["horizon_minutes"] for r in result["predictions"]], [30, 60])
        self.assertTrue(all(r["model_mode"] == "real" for r in result["predictions"]))
        json.dumps(result, allow_nan=False)

    def test_real_model_insufficient_history_is_blocked(self):
        self.assertEqual(self.loaded["status"], "ready", self.loaded)
        rows = sequence(name=self.loaded["categories"][0], count=2)
        result = self.service.predict([rows[-1]], rows[:-1])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["predictions"], [])
        self.assertIn("insufficient_history", [d["code"] for d in result["diagnostics"]])


if __name__ == "__main__":
    unittest.main()
