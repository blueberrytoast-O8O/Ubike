import copy
import json
import math
import unittest

from backend.pipeline import run_realtime_pipeline

NOW = "2026-09-12T22:30:00+08:00"


def station(sid="A", bikes=1, docks=9, lat=25.0, lon=121.0):
    return dict(official_station_id=sid, station_name=sid, district="D",
                observed_at=NOW, total_slots=bikes+docks,
                available_bikes=bikes, available_docks=docks,
                latitude=lat, longitude=lon)


def alert_result(sources=None, status="ok"):
    sources = sources or ["realtime"]
    return dict(status=status, active_alerts=[dict(alert_id="AL-A", official_station_id="A",
        station_name="A", alert_type="low_bikes", severity="critical", status="active",
        observed_at=NOW, first_seen_at=NOW, last_seen_at=NOW, duration_minutes=0,
        current_bikes=1, current_docks=9, total_slots=10, triggered_by=sources,
        forecast_horizons=[30] if "forecast_30m" in sources else [], reason_codes=[], diagnostics=[])],
        resolved_alerts=[], diagnostics=[], prediction_status="ready",
        alert_summary=dict(active_count=1))


def optimizer_result(plans=None, diagnostics=None):
    return dict(candidate_plans=plans or [], excluded_alerts=[], unmet_demand=[],
                diagnostics=diagnostics or [], requires_admin_confirmation=True,
                optimization_summary=dict(candidate_count=len(plans or []), prediction_status="ready"))


class StubModelService:
    status = "ready"
    predictions = [
        dict(official_station_id="A", station_name="A", observed_at=NOW,
             predicted_at="2026-09-12T23:00:00+08:00", horizon_minutes=30,
             raw_prediction=1.2, predicted_bikes=1, total_slots=10,
             model_mode="real", diagnostics=[]),
        dict(official_station_id="A", station_name="A", observed_at=NOW,
             predicted_at="2026-09-12T23:30:00+08:00", horizon_minutes=60,
             raw_prediction=2.2, predicted_bikes=2, total_slots=10,
             model_mode="real", diagnostics=[]),
    ]

    def __init__(self, *args, **kwargs):
        pass

    def predict(self, current, history):
        return dict(status=self.status, predictions=copy.deepcopy(self.predictions),
                    diagnostics=[], blocked_stations=[])


class PipelineTests(unittest.TestCase):
    def deps(self, **overrides):
        deps = dict(
            normalize_snapshots=lambda source: dict(stations=[station()], diagnostics=[],
                                                    selected_source_file="snapshot.csv",
                                                    latest_observed_at=NOW,
                                                    timezone="Asia/Taipei"),
            build_mapping=lambda stations: dict(mapping={"A": "A"}, diagnostics=[]),
            load_history_records=lambda source, mapping, end=None: dict(records=[station()], diagnostics=[]),
            model_service_class=StubModelService,
            evaluate_alerts=lambda *a, **k: alert_result(),
            optimize=lambda *a, **k: optimizer_result(),
        )
        deps.update(overrides)
        return deps

    def run_pipeline(self, **kwargs):
        return run_realtime_pipeline("x.csv", now=NOW, dependencies=self.deps(), **kwargs)

    def test_full_happy_path(self):
        result = self.run_pipeline(model_pack_path="model.pkl", historical_data_source="hist")
        self.assertEqual(result["pipeline_status"], "ready")
        self.assertEqual(len(result["prediction"]["horizon_30m"]), 1)
        self.assertEqual(len(result["prediction"]["horizon_60m"]), 1)

    def test_realtime_success_prediction_ready(self):
        self.assertEqual(self.run_pipeline(model_pack_path="model.pkl", historical_data_source="hist")["prediction"]["status"], "ready")

    def test_realtime_success_prediction_partial(self):
        class Partial(StubModelService):
            status = "partial"
        result = run_realtime_pipeline("x.csv", now=NOW, model_pack_path="model.pkl", historical_data_source="hist",
                                       dependencies=self.deps(model_service_class=Partial))
        self.assertEqual(result["pipeline_status"], "degraded")
        self.assertEqual(result["prediction"]["status"], "partial")

    def test_realtime_success_prediction_blocked(self):
        class Blocked(StubModelService):
            def predict(self, current, history):
                return dict(status="blocked", predictions=[], diagnostics=[dict(code="no_eligible_stations", message="blocked")])
        result = run_realtime_pipeline("x.csv", now=NOW, model_pack_path="model.pkl", historical_data_source="hist",
                                       dependencies=self.deps(model_service_class=Blocked))
        self.assertEqual(result["pipeline_status"], "degraded")
        self.assertEqual(result["prediction"]["status"], "blocked")

    def test_prediction_blocked_still_realtime_alerts(self):
        result = self.run_pipeline(model_pack_path="model.pkl")
        self.assertEqual(result["prediction"]["status"], "blocked")
        self.assertEqual(len(result["alerts"]["active_alerts"]), 1)

    def test_prediction_blocked_no_fake_forecast_alerts(self):
        seen = {}
        def alerts(stations, predictions=None, prediction_status=None, **kwargs):
            seen["predictions"] = predictions
            seen["prediction_status"] = prediction_status
            return alert_result(["realtime"])
        result = run_realtime_pipeline("x.csv", now=NOW, model_pack_path="model.pkl",
                                       dependencies=self.deps(evaluate_alerts=alerts))
        self.assertEqual(seen["predictions"], [])
        self.assertEqual(result["alerts"]["active_alerts"][0]["triggered_by"], ["realtime"])

    def test_no_vehicle_degraded_not_crash(self):
        result = run_realtime_pipeline("x.csv", now=NOW, dependencies=self.deps(
            optimize=lambda *a, **k: optimizer_result(diagnostics=[dict(code="no_usable_vehicle", message="")])
        ))
        self.assertEqual(result["optimization"]["candidate_plans"], [])
        self.assertEqual(result["pipeline_status"], "degraded")

    def test_coordinate_enrichment_works(self):
        reference = {"station_reference": {"A": {"latitude": 25.1, "longitude": 121.1}}}
        result = run_realtime_pipeline("x.csv", now=NOW, station_coordinate_reference=reference,
                                       dependencies=self.deps(normalize_snapshots=lambda source: dict(
                                           stations=[station(lat=None, lon=None)], diagnostics=[],
                                           selected_source_file="snapshot.csv", latest_observed_at=NOW, timezone="Asia/Taipei")))
        self.assertEqual((result["stations"][0]["latitude"], result["stations"][0]["longitude"]), (25.1, 121.1))

    def test_station_missing_coordinates_only_affects_optimizer(self):
        result = run_realtime_pipeline("x.csv", now=NOW, dependencies=self.deps(
            normalize_snapshots=lambda source: dict(stations=[station(lat=None, lon=None)], diagnostics=[],
                                                    selected_source_file="snapshot.csv", latest_observed_at=NOW, timezone="Asia/Taipei"),
            optimize=lambda *a, **k: optimizer_result(diagnostics=[dict(code="missing_or_invalid_coordinates", message="A")])
        ))
        self.assertEqual(len(result["stations"]), 1)
        self.assertTrue(any(d["code"] == "missing_or_invalid_coordinates" for d in result["optimization"]["diagnostics"]))

    def test_snapshot_failure_with_previous_state_stale(self):
        previous = dict(stations=[station()], source_snapshot=dict(selected_source_file="old.csv"),
                        alerts=alert_result())
        result = run_realtime_pipeline("bad.csv", now=NOW, previous_successful_state=previous,
                                       dependencies=self.deps(normalize_snapshots=lambda source: (_ for _ in ()).throw(ValueError("bad"))))
        self.assertTrue(result["stale"])
        self.assertEqual(result["pipeline_status"], "degraded")

    def test_snapshot_failure_without_previous_state_blocked(self):
        result = run_realtime_pipeline("bad.csv", now=NOW,
                                       dependencies=self.deps(normalize_snapshots=lambda source: dict(stations=[], diagnostics=[])))
        self.assertEqual(result["pipeline_status"], "blocked")

    def test_alert_engine_failure_returns_station_state(self):
        result = run_realtime_pipeline("x.csv", now=NOW, dependencies=self.deps(
            evaluate_alerts=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("alert fail"))))
        self.assertEqual(len(result["stations"]), 1)
        self.assertEqual(result["pipeline_status"], "degraded")

    def test_optimizer_failure_returns_alerts(self):
        result = run_realtime_pipeline("x.csv", now=NOW, dependencies=self.deps(
            optimize=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("optimizer fail"))))
        self.assertEqual(len(result["alerts"]["active_alerts"]), 1)
        self.assertEqual(result["optimization"]["candidate_plans"], [])

    def test_haversine_fallback_label(self):
        result = self.run_pipeline()
        self.assertEqual(result["optimization"]["route_estimation_method"], "haversine")

    def test_requires_admin_confirmation(self):
        self.assertTrue(self.run_pipeline()["optimization"]["requires_admin_confirmation"])

    def test_input_not_mutated(self):
        current = [station()]
        original = copy.deepcopy(current)
        run_realtime_pipeline(current_records=current, now=NOW, dependencies=self.deps())
        self.assertEqual(current, original)

    def test_strict_json_serialization(self):
        json.dumps(self.run_pipeline(), allow_nan=False)

    def test_no_nan_or_infinity(self):
        def bad_optimizer(*args, **kwargs):
            return optimizer_result(plans=[dict(plan_id="P", estimated_total_distance_km=math.inf)])
        result = run_realtime_pipeline("x.csv", now=NOW, dependencies=self.deps(optimize=bad_optimizer))
        self.assertTrue(any(d["code"] == "optimizer_failed" for d in result["optimization"]["diagnostics"]))

    def test_repeated_run_deterministic(self):
        self.assertEqual(self.run_pipeline(), self.run_pipeline())

    def test_station_level_error_isolation(self):
        result = run_realtime_pipeline("x.csv", now=NOW, dependencies=self.deps(
            normalize_snapshots=lambda source: dict(stations=[station("A"), station("B")],
                                                    diagnostics=[dict(code="invalid_station", message="bad C")],
                                                    selected_source_file="snapshot.csv", latest_observed_at=NOW, timezone="Asia/Taipei")))
        self.assertEqual(len(result["stations"]), 2)
        self.assertTrue(any(d["source_module"] == "realtime_normalizer" for d in result["diagnostics"]))


if __name__ == "__main__":
    unittest.main()
