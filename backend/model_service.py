"""Lazy local joblib model service. No implicit mock or global model instance.

ModelService(path=None) uses path or YOUBIKE_MODEL_PATH; accepts a trusted pkl
or a ZIP with exactly one pkl member, loaded by joblib.load without extraction.
predict(current, history) builds features using the loaded category contract.
predict_features(frame, stations) is the strict precomputed-feature entry point.
All public service results are strict-JSON-safe. Model load/predict failures
return blocked, never persistence predictions. Explicit mode='mock' uses current
bikes for BOTH horizons and labels every result mock; categories must be given.

Integer output rule: clip raw to [0, total_slots], then floor(value + 0.5)
(round half up). raw_prediction remains the original finite model float.
"""

import importlib
import io
import json
import math
import os
from pathlib import Path
import warnings
import zipfile

from backend.feature_engineering import (FEATURES, DTYPES, build_features, _timestamp, _count,
    DEFAULT_ALIGNMENT_TOLERANCE_SECONDS, align_model_time_slot)


def _diagnostic(code, message):
    return dict(code=code, message=str(message), severity="error")


class ModelService:
    def __init__(self, model_path=None, *, mode="real", mock_categories=None,
                 alignment_tolerance_seconds=DEFAULT_ALIGNMENT_TOLERANCE_SECONDS,
                 holiday_calendar_path=None):
        self.model_path = model_path
        self.mode = mode
        self.alignment_tolerance_seconds = alignment_tolerance_seconds
        self.holiday_calendar_path = holiday_calendar_path
        self._mock_categories = mock_categories
        self._models = None
        self._categories = None
        self._dependencies = None

    def _blocked(self, code, message, diagnostics=(), blocked_stations=()):
        return dict(status="blocked", model_mode=self.mode, predictions=[],
                    diagnostics=list(diagnostics) + [_diagnostic(code, message)],
                    blocked_stations=list(blocked_stations))

    def _imports(self):
        if self._dependencies is None:
            names = ("numpy", "pandas") if self.mode == "mock" else ("numpy", "pandas", "scipy", "sklearn", "lightgbm", "joblib")
            self._dependencies = {name: importlib.import_module(name) for name in names}
        return self._dependencies

    def load(self):
        """Load on demand; validate BOTH models and exact persisted categories."""
        if self.mode not in ("real", "mock"):
            return self._blocked("invalid_model_mode", self.mode)
        if self._categories is not None:
            return dict(status="ready", model_mode=self.mode, categories=list(self._categories), diagnostics=[])
        path = self.model_path or os.environ.get("YOUBIKE_MODEL_PATH")
        if self.mode == "real" and not path:
            return self._blocked("model_path_missing", "Provide model_path or YOUBIKE_MODEL_PATH")
        try:
            if self.mode == "real" and not Path(path).is_file():
                return self._blocked("model_path_not_found", str(path))
            dependencies = self._imports()
            if self.mode == "mock":
                categories = list(self._mock_categories or [])
                models = {}
            else:
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    if zipfile.is_zipfile(path):
                        with zipfile.ZipFile(path) as archive:
                            names = [n for n in archive.namelist() if n.lower().endswith(".pkl") and not n.endswith("/")]
                            if len(names) != 1:
                                return self._blocked("invalid_model_archive", "ZIP must contain exactly one pkl")
                            pack = dependencies["joblib"].load(io.BytesIO(archive.read(names[0])))
                    else:
                        pack = dependencies["joblib"].load(path)
                if any(w.category.__name__ == "InconsistentVersionWarning" for w in caught):
                    return self._blocked("model_version_mismatch", "; ".join(str(w.message) for w in caught))
                if not isinstance(pack, dict) or not isinstance(pack.get("models"), dict):
                    return self._blocked("invalid_model_pack", "Expected pack.models dictionary")
                models = pack["models"]
                if any(k not in models for k in ("30min", "60min")):
                    return self._blocked("missing_horizon_model", "Both models.30min and models.60min are required")
                if pack.get("features") != list(FEATURES) or pack.get("sampling_interval") != "30min" or pack.get("target_type") != "absolute":
                    return self._blocked("model_contract_mismatch", "Features, sampling_interval or target_type mismatch")
                categories = pack.get("stations")
                if not isinstance(categories, list):
                    return self._blocked("model_category_mismatch", "pack.stations must be a list")
                for key in ("30min", "60min"):
                    model = models[key]
                    if not callable(getattr(model, "predict", None)) or list(model.feature_name_) != list(FEATURES):
                        return self._blocked("model_contract_mismatch", key + " feature names mismatch")
                    if model.booster_.pandas_categorical != [categories]:
                        return self._blocked("model_category_mismatch", key + " categories disagree with pack.stations")
            if not categories or any(not isinstance(c, str) or not c for c in categories) or len(categories) != len(set(categories)):
                return self._blocked("model_category_mismatch", "Nonempty unique station-name categories required")
            self._models, self._categories = models, list(categories)
            return dict(status="ready", model_mode=self.mode, categories=list(categories), diagnostics=[])
        except (ImportError, ModuleNotFoundError) as exc:
            return self._blocked("dependency_missing", str(exc))
        except Exception as exc:
            return self._blocked("model_load_failed", f"{type(exc).__name__}: {exc}")

    def predict(self, current, history):
        loaded = self.load()
        if loaded["status"] != "ready":
            return loaded
        try:
            batch = build_features(current, history, self._categories,
                alignment_tolerance_seconds=self.alignment_tolerance_seconds,
                holiday_calendar_path=self.holiday_calendar_path)
        except (ImportError, ModuleNotFoundError) as exc:
            return self._blocked("dependency_missing", str(exc))
        except Exception as exc:
            return self._blocked("feature_generation_failed", f"{type(exc).__name__}: {exc}")
        if batch["status"] == "blocked":
            return self._blocked("no_eligible_stations", "No station has complete valid features",
                                 batch["diagnostics"], batch["blocked_stations"])
        result = self.predict_features(batch["features"], batch["stations"])
        result["diagnostics"] = batch["diagnostics"] + result["diagnostics"]
        result["blocked_stations"] = batch["blocked_stations"]
        if batch["blocked_stations"] and result["status"] == "ready":
            result["status"] = "partial"
        return result

    def predict_features(self, frame, stations):
        loaded = self.load()
        if loaded["status"] != "ready":
            return loaded
        try:
            pd, np = self._dependencies["pandas"], self._dependencies["numpy"]
            if not isinstance(frame, pd.DataFrame) or list(frame.columns) != list(FEATURES):
                return self._blocked("feature_schema_mismatch", "Expected exact 38 feature names and order")
            if len(frame) == 0 or len(frame) != len(stations):
                return self._blocked("feature_metadata_mismatch", "Nonempty aligned station metadata required")
            for key, dtype in DTYPES.items():
                if str(frame[key].dtype) != dtype:
                    return self._blocked("feature_dtype_mismatch", f"{key}: expected {dtype}, got {frame[key].dtype}")
            if frame.station_id.cat.categories.tolist() != self._categories or frame.station_id.cat.ordered:
                return self._blocked("model_category_mismatch", "Category values/order/ordered flag do not match model")
            if frame.station_id.isna().any() or any(s.get("station_name") not in self._categories for s in stations):
                return self._blocked("unknown_model_station", "Unknown station name or missing categorical value")
            if not np.isfinite(frame.drop(columns="station_id").to_numpy()).all():
                return self._blocked("undefined_features", "Training contract excludes NaN and Inf")
            clean_metadata = []
            for index, station in enumerate(stations):
                if frame.iloc[index]["station_id"] != station["station_name"]:
                    return self._blocked("feature_metadata_mismatch", "station_name does not match feature row")
                sid = station.get("official_station_id")
                if not isinstance(sid, str) or not sid:
                    return self._blocked("feature_metadata_mismatch", "Official station ID must be a nonempty string")
                total = _count(station["total_slots"])
                if total != frame.iloc[index]["total_slots"]:
                    return self._blocked("feature_metadata_mismatch", "total_slots mismatch")
                stamp = _timestamp(station["observed_at"])
                try:
                    slot = align_model_time_slot(stamp, self.alignment_tolerance_seconds)
                except ValueError as exc:
                    return self._blocked("off_schedule_snapshot", str(exc))
                if "model_time_slot" in station and _timestamp(station["model_time_slot"]) != slot:
                    return self._blocked("feature_metadata_mismatch", "model_time_slot does not match observed_at alignment")
                try:
                    json.dumps(station.get("diagnostics", []), allow_nan=False)
                except (TypeError, ValueError):
                    return self._blocked("invalid_metadata", "Station diagnostics must be strict-JSON-safe")
                clean_metadata.append((sid, station["station_name"], stamp, total))
            # Evaluate both horizons before returning any predictions. A failure
            # in one model never substitutes the other model's predictions.
            outputs = {}
            for minutes in (30, 60):
                if self.mode == "mock":
                    values = frame.bikes.to_numpy(dtype=float)
                else:
                    model = self._models[f"{minutes}min"]
                    values = np.asarray(model.predict(frame, num_iteration=model.best_iteration_), dtype=float)
                if values.shape != (len(frame),) or not np.isfinite(values).all():
                    return self._blocked("invalid_model_prediction", f"{minutes}min returned invalid shape or nonfinite values")
                outputs[minutes] = values
            predictions = []
            for index, (sid, name, observed, total) in enumerate(clean_metadata):
                for minutes in (30, 60):
                    raw = float(outputs[minutes][index])
                    predictions.append(dict(official_station_id=sid, station_name=name,
                        observed_at=stations[index]["observed_at"],
                        model_time_slot=align_model_time_slot(observed, self.alignment_tolerance_seconds).isoformat(),
                        predicted_at=(observed + pd.Timedelta(minutes=minutes)).isoformat(),
                        horizon_minutes=minutes, raw_prediction=raw,
                        predicted_bikes=math.floor(min(total, max(0, raw)) + 0.5),
                        total_slots=total, model_mode=self.mode,
                        diagnostics=list(stations[index].get("diagnostics", []))))
            return dict(status="ready", model_mode=self.mode, predictions=predictions,
                        diagnostics=[], blocked_stations=[])
        except (ImportError, ModuleNotFoundError) as exc:
            return self._blocked("dependency_missing", str(exc))
        except Exception as exc:
            return self._blocked("inference_failed", f"{type(exc).__name__}: {exc}")
