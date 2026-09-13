"""CLI contracts; synthetic fixtures validate mechanics, not model accuracy."""
import contextlib
import copy
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from backend import demo

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS = [str(ROOT / "tests/fixtures" / name) for name in ("snapshot_2200.csv", "snapshot_2230.csv")]


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "result.json"

    def call(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = demo.main(args)
        return code, out.getvalue(), err.getvalue()

    def realtime(self, *args):
        return self.call(["realtime", "--realtime-csv", *SNAPSHOTS, *args])

    def result(self):
        return demo.run_demo(demo.build_parser().parse_args(["realtime", "--realtime-csv", *SNAPSHOTS]))

    def replay_result(self):
        comparisons = [dict(station_name=name, official_station_id=name, horizon_minutes=h,
                            predicted_bikes=2, actual_bikes=3) for name in "ZYXWVUA" for h in (60, 30)]
        return dict(mode="replay", origin="2026-06-15T12:00:00+08:00", eligible_stations=7,
                    evaluated_stations=7, excluded_stations=[], evaluation_status="ready",
                    metric_basis="predicted_bikes", prediction=dict(status="ready", diagnostics=[]),
                    comparisons=comparisons, metrics={"30": {"MAE": 1.0}, "60": {"MAE": 1.0}},
                    alerts=dict(active_alerts=[]), diagnostics=[])

    def test_help_subprocess(self):
        result = subprocess.run([sys.executable, "-m", "backend.demo", "--help"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("replay", result.stdout)

    def test_compact_summary(self):
        code, out, err = self.realtime()
        self.assertEqual(code, 0, err)
        self.assertIn("Mode: Realtime", out)
        self.assertLess(len(out), 5000)

    def test_degraded_exit_zero(self):
        code, out, _ = self.realtime()
        self.assertEqual(code, 0)
        self.assertIn("Pipeline status: DEGRADED", out)

    def test_blocked_predictions_never_fabricated(self):
        args = demo.build_parser().parse_args(["realtime", "--realtime-csv", *SNAPSHOTS, "--model-pack", SNAPSHOTS[0]])
        with patch.object(demo, "_load_model"):
            result = demo.run_demo(args)
        self.assertEqual(result["prediction"]["status"], "blocked")
        self.assertEqual(result["prediction"]["horizon_30m"], [])
        self.assertEqual(result["prediction"]["horizon_60m"], [])

    def test_alerts_remain_visible(self):
        result = self.result()
        self.assertTrue(result["alerts"]["active_alerts"])
        self.assertIn(f"Active alerts: {len(result['alerts']['active_alerts'])}", demo.render_summary(result))

    def test_no_vehicle_is_success(self):
        code, out, _ = self.realtime()
        self.assertEqual(code, 0)
        self.assertIn("no_usable_vehicle", out)

    def test_coordinates_after_enrichment(self):
        reference = Path(self.temp.name) / "coordinates.csv"
        stations = self.result()["stations"]
        reference.write_text("datetime,station_name,lng,lat\n" + "".join(
            f"2026-06-15 12:00:00,{s['station_name']},121,25\n" for s in stations), encoding="utf-8")
        code, out, err = self.realtime("--coordinate-source", str(reference))
        self.assertEqual(code, 0, err)
        self.assertIn(f"Coordinates: {len(stations)} / {len(stations)} (final enriched state)", out)

    def test_upstream_coordinate_diagnostics_labeled(self):
        result = self.result()
        for station in result["stations"]:
            station.update(latitude=25, longitude=121)
        result["diagnostics"].append(dict(source_module="realtime_normalizer", code="missing_coordinate"))
        out = demo.render_summary(result)
        self.assertIn("not final coordinate counts", out)
        self.assertIn("realtime_normalizer: missing_coordinate", out)

    def test_replay_label(self):
        out = demo.render_summary(self.replay_result())
        self.assertIn("HISTORICAL REPLAY", out)
        self.assertNotIn("Mode: Realtime", out)

    def test_replay_metrics_label(self):
        out = demo.render_summary(self.replay_result())
        self.assertIn("Historical replay evaluation", out)
        for forbidden in ("production accuracy", "realtime accuracy", "live prediction accuracy"):
            self.assertNotIn(forbidden, out)

    def test_examples_deterministic_and_at_most_five(self):
        result = self.replay_result()
        out = demo.render_summary(result)
        result["comparisons"].reverse()
        self.assertEqual(out, demo.render_summary(result))
        self.assertEqual(out.count("30m 2 / 3"), 5)
        self.assertLess(out.index("A (A)"), out.index("U (U)"))

    def test_strict_json_datetime_and_nonfinite(self):
        result = self.result()
        result["extra"] = [datetime(2026, 6, 15), float("nan"), float("inf"), -float("inf")]
        with patch.object(demo, "run_demo", return_value=result):
            code, _, err = self.realtime("--output-json", str(self.output))
        self.assertEqual(code, 0, err)
        parsed = json.loads(self.output.read_text(encoding="utf-8"), parse_constant=lambda value: self.fail(value))
        self.assertEqual(parsed["extra"], ["2026-06-15T00:00:00", None, None, None])

    def test_json_utf8_complete_result(self):
        code, _, err = self.realtime("--output-json", str(self.output))
        self.assertEqual(code, 0, err)
        text = self.output.read_text(encoding="utf-8")
        self.assertNotIn("\\u", text)
        self.assertEqual(len(json.loads(text)["stations"]), len(self.result()["stations"]))

    def test_missing_input_nonzero(self):
        code, _, err = self.call(["realtime", "--realtime-csv", "missing.csv"])
        self.assertEqual(code, 1)
        self.assertIn("Input path does not exist", err)

    def test_missing_explicit_optional_path_nonzero(self):
        self.assertEqual(self.realtime("--coordinate-source", "missing.csv")[0], 1)

    def test_pipeline_blocked_nonzero(self):
        empty = Path(self.temp.name) / "empty.csv"
        empty.write_text("invalid\n", encoding="utf-8")
        code, out, _ = self.call(["realtime", "--realtime-csv", str(empty)])
        self.assertEqual(code, 1)
        self.assertIn("Pipeline status: BLOCKED", out)

    def test_inputs_unchanged(self):
        before = [Path(p).read_bytes() for p in SNAPSHOTS]
        self.realtime("--output-json", str(self.output))
        self.assertEqual(before, [Path(p).read_bytes() for p in SNAPSHOTS])

    def test_core_modules_unchanged(self):
        paths = [p for p in (ROOT / "backend").rglob("*") if p.suffix in (".py", ".json") and p.name != "demo.py"]
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        self.realtime()
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})

    def test_stdout_not_station_dump(self):
        _, out, _ = self.realtime()
        self.assertNotIn('"stations":', out)
        self.assertNotIn('"official_station_id":', out)

    def test_output_cannot_overwrite_input(self):
        before = Path(SNAPSHOTS[0]).read_bytes()
        self.assertEqual(self.realtime("--output-json", SNAPSHOTS[0])[0], 1)
        self.assertEqual(before, Path(SNAPSHOTS[0]).read_bytes())

    def test_bad_model_nonzero(self):
        self.assertEqual(self.realtime("--model-pack", SNAPSHOTS[0])[0], 1)

    def test_unwritable_output_nonzero(self):
        self.assertEqual(self.realtime("--output-json", str(self.output / "missing"))[0], 1)

    def test_replay_no_evaluation_nonzero(self):
        result = self.replay_result()
        result["evaluation_status"] = "blocked"
        with patch.object(demo, "run_demo", return_value=result):
            self.assertEqual(self.call(["replay", "--history-source", "h", "--model-pack", "p", "--origin", "t"])[0], 1)

    def test_replay_integration_no_future_leakage_and_metrics(self):
        args = demo.build_parser().parse_args(["replay", "--history-source", "h", "--model-pack", "p",
                                              "--origin", "2026-06-15T12:00:00+08:00", "--realtime-csv", *SNAPSHOTS])
        rows = [dict(official_station_id="A", station_name="A", model_time_slot=f"2026-06-15T{t}+08:00",
                     observed_at=f"2026-06-15T{t}+08:00", total_slots=20, available_bikes=b,
                     latitude=25, longitude=121) for t, b in (("11:30:00", 1), ("12:00:00", 2), ("12:30:00", 3), ("13:00:00", 5))]
        class Service:
            def predict(inner, current, history):
                self.assertEqual([r["available_bikes"] for r in current], [2])
                self.assertEqual([r["available_bikes"] for r in history], [1])
                return dict(status="ready", diagnostics=[], predictions=[dict(
                    rows[1], horizon_minutes=h, predicted_bikes=2, raw_prediction=2.25, model_mode="real",
                    predicted_at=rows[i]["observed_at"]) for h, i in ((30, 2), (60, 3))])
        with patch.object(demo, "load_history_records", return_value=dict(records=rows, diagnostics=[])):
            result = demo._replay(args, Service())
        self.assertEqual(result["evaluated_stations"], 1)
        self.assertEqual(result["metrics"]["30"]["MAE"], 0.75)
        self.assertEqual(result["metrics"]["60"]["RMSE"], 2.75)

    def test_summary_does_not_mutate_result(self):
        result = self.result()
        before = copy.deepcopy(result)
        demo.render_summary(result)
        self.assertEqual(result, before)


if __name__ == "__main__":
    unittest.main()
