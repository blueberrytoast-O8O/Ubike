"""Small synthetic half-hour histories; no production history is read."""
import json
import tempfile
from pathlib import Path
import math
import unittest

import numpy as np
import pandas as pd

from backend.feature_engineering import build_features, _station_features, FEATURES, DTYPES, align_model_time_slot


def sequence(name="Alpha", sid="001", count=673, offset=0, end="2026-05-20T12:30:00+08:00"):
    times = pd.date_range(end=end, periods=count, freq="30min")
    return [dict(official_station_id=sid, station_name=name, observed_at=t.isoformat(),
                 available_bikes=10 + offset + i % 37, total_slots=100)
            for i, t in enumerate(times)]


def kernel(rows):
    return _station_features([dict(r, model_time_slot=align_model_time_slot(r["observed_at"])) for r in rows], ["Alpha", "Beta"])


class FeatureEngineeringTests(unittest.TestCase):
    def setUp(self):
        self.rows = sequence()

    def build(self, rows=None):
        rows = self.rows if rows is None else rows
        return build_features([rows[-1]], rows[:-1], ["Alpha", "Beta"])

    def test_station_lags_are_independent_and_sorted(self):
        other = sequence("Beta", "900", offset=40)
        result = build_features([self.rows[-1], other[-1]], list(reversed(self.rows[:-1] + other[:-1])), ["Alpha", "Beta"])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["features"].lag_30m.tolist(), [self.rows[-2]["available_bikes"], other[-2]["available_bikes"]])

    def test_all_seven_lags(self):
        row = self.build()["features"].iloc[0]
        for name, step in [("lag_30m", 1), ("lag_1h", 2), ("lag_2h", 4), ("lag_24h", 48), ("lag_48h", 96), ("lag_1w", 336), ("lag_2w", 672)]:
            with self.subTest(name=name):
                self.assertEqual(row[name], self.rows[-1-step]["available_bikes"])

    def test_rolling_excludes_current(self):
        before = self.build()["features"].iloc[0]
        self.rows[-1]["available_bikes"] = 99
        row = self.build()["features"].iloc[0]
        for key in [f for f in FEATURES if f.startswith("rolling_")]:
            self.assertEqual(row[key], before[key])
        for hours, n in [(1, 2), (2, 4), (3, 6)]:
            self.assertAlmostEqual(row[f"rolling_mean_{hours}h"], np.mean([r["available_bikes"] for r in self.rows[-n-1:-1]]))
        previous = [r["available_bikes"] for r in self.rows[-5:-1]]
        self.assertAlmostEqual(row.rolling_std_2h, np.std(previous, ddof=1))
        self.assertEqual(row.rolling_min_2h, min(previous))
        self.assertEqual(row.rolling_max_2h, max(previous))

    def test_rolling_min_periods(self):
        frame = kernel(self.rows[:5])
        self.assertTrue(math.isnan(frame.iloc[1].rolling_std_2h))
        self.assertFalse(math.isnan(frame.iloc[2].rolling_std_2h))
        self.assertTrue(math.isnan(frame.iloc[3].rolling_mean_2h))
        self.assertFalse(math.isnan(frame.iloc[4].rolling_mean_2h))

    def test_diff_and_change_rates(self):
        row = self.build()["features"].iloc[0]
        for suffix, step in [("30m", 1), ("1h", 2), ("2h", 4)]:
            diff = self.rows[-1]["available_bikes"] - self.rows[-1-step]["available_bikes"]
            self.assertEqual(row["diff_" + suffix], diff)
            if suffix != "2h":
                self.assertAlmostEqual(row["change_rate_" + suffix], diff / self.rows[-1-step]["available_bikes"])

    def test_zero_denominators_remain_nan_and_block(self):
        self.rows[-2]["available_bikes"] = 0
        self.rows[-3]["available_bikes"] = 0
        frame = kernel(self.rows)
        self.assertTrue(math.isnan(frame.iloc[-1].change_rate_30m))
        self.assertTrue(math.isnan(frame.iloc[-1].change_rate_1h))
        result = self.build()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("undefined_features", [d["code"] for d in result["diagnostics"]])

    def test_calendar_cycles(self):
        row = self.build()["features"].iloc[0]
        self.assertEqual([row[k] for k in ["month", "day", "hour", "minute", "day_of_week", "time_slot", "week_slot"]], [5, 20, 12, 30, 2, 25, 121])
        for key, expected in [("hour_sin", math.sin(2*math.pi*25/48)), ("hour_cos", math.cos(2*math.pi*25/48)), ("week_sin", math.sin(2*math.pi*121/336)), ("week_cos", math.cos(2*math.pi*121/336))]:
            self.assertAlmostEqual(row[key], expected)

    def test_missing_half_hour_is_diagnosed(self):
        rows = sequence(count=674)
        del rows[100]
        result = self.build(rows)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("non_contiguous_history", [d["code"] for d in result["diagnostics"]])

    def test_short_history_only_blocks_one_station(self):
        other = sequence("Beta", "002", count=4)
        result = build_features([self.rows[-1], other[-1]], self.rows[:-1] + other[:-1], ["Alpha", "Beta"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["features"].station_id.tolist(), ["Alpha"])
        self.assertEqual(result["blocked_stations"][0]["station_name"], "Beta")

    def test_identity_category_order_columns_and_dtypes(self):
        result = build_features([self.rows[-1]], self.rows[:-1], ["Beta", "Alpha"])
        frame = result["features"]
        expected = """station_id total_slots month day hour minute day_of_week time_slot week_slot
        hour_sin hour_cos week_sin week_cos is_weekend is_national_holiday is_day_off
        is_day_before_holiday is_day_after_holiday bikes bike_ratio lag_30m lag_1h lag_2h
        lag_24h lag_48h lag_1w lag_2w diff_30m diff_1h diff_2h change_rate_30m change_rate_1h
        rolling_mean_1h rolling_mean_2h rolling_mean_3h rolling_std_2h rolling_min_2h rolling_max_2h""".split()
        self.assertEqual(list(frame.columns), expected)
        self.assertEqual(len(frame.columns), 38)
        self.assertEqual({k: str(t) for k, t in frame.dtypes.items()}, DTYPES)
        self.assertEqual(frame.station_id.cat.categories.tolist(), ["Beta", "Alpha"])
        self.assertEqual(frame.station_id.iloc[0], "Alpha")
        self.assertEqual(result["stations"][0]["official_station_id"], "001")

    def test_unknown_station(self):
        result = build_features([self.rows[-1]], self.rows[:-1], ["Other"])
        self.assertEqual(result["status"], "blocked")
        self.assertIn("unknown_model_station", [d["code"] for d in result["diagnostics"]])

    def test_holiday_formulas_and_local_date(self):
        for end, expected in [("2026-04-02T16:00:00Z", (0, 1, 1, 1, 0)), ("2026-04-07T12:00:00+08:00", (0, 0, 0, 0, 1)), ("2026-05-09T12:00:00+08:00", (1, 0, 1, 0, 0))]:
            row = self.build(sequence(end=end))["features"].iloc[0]
            self.assertEqual(tuple(row[k] for k in ["is_weekend", "is_national_holiday", "is_day_off", "is_day_before_holiday", "is_day_after_holiday"]), expected)

    def test_calendar_out_of_range_blocks(self):
        result = self.build(sequence(end="2026-09-12T22:30:29+08:00"))
        self.assertEqual(result["status"], "blocked")
        self.assertIn("holiday_calendar_out_of_range", [d["code"] for d in result["diagnostics"]])

    def test_duplicate_time_blocks_station(self):
        old = dict(self.rows[-1], available_bikes=1)
        result = build_features([self.rows[-1]], self.rows[:-1] + [old], ["Alpha"])
        self.assertEqual(result["status"], "blocked")
        self.assertIn("duplicate_model_time_slot", [d["code"] for d in result["diagnostics"]])

    def test_bad_numeric_and_id_collision_block(self):
        for key, value in [("available_bikes", "oops"), ("official_station_id", "wrong")]:
            rows = sequence()
            rows[100][key] = value
            result = self.build(rows)
            self.assertEqual(result["status"], "blocked")

    def test_no_future_leakage_and_input_not_mutated(self):
        import copy
        before = copy.deepcopy(self.rows)
        future = dict(self.rows[-1], observed_at="2026-05-20T13:00:00+08:00", available_bikes=99)
        result = build_features([self.rows[-1]], self.rows[:-1] + [future], ["Alpha"])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["features"].iloc[0].bikes, before[-1]["available_bikes"])
        self.assertEqual(self.rows, before)

    def test_crawler_seconds_preserved_with_aligned_slot(self):
        self.rows[-1]["observed_at"] = "2026-05-20T12:30:20+08:00"
        result = self.build()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["stations"][0]["observed_at"], self.rows[-1]["observed_at"])
        self.assertEqual(result["stations"][0]["model_time_slot"], "2026-05-20T12:30:00+08:00")

    def test_real_snapshot_drift_is_contiguous_but_insufficient(self):
        rows = sequence(count=2)
        rows[0]["observed_at"] = "2026-09-12T22:00:09+08:00"
        rows[1]["observed_at"] = "2026-09-12T22:30:29+08:00"
        result = self.build(rows)
        codes = [d["code"] for d in result["diagnostics"]]
        self.assertNotIn("non_contiguous_history", codes)
        self.assertNotIn("off_schedule_snapshot", codes)
        self.assertIn("insufficient_history", codes)
        self.assertIn("holiday_calendar_out_of_range", codes)

    def test_2214_is_off_schedule(self):
        self.rows[-1]["observed_at"] = "2026-05-20T22:14:00+08:00"
        result = self.build()
        self.assertIn("off_schedule_snapshot", [d["code"] for d in result["diagnostics"]])
        self.assertIsNone(result["blocked_stations"][0]["model_time_slot"])

    def test_tolerance_is_configurable_and_inclusive(self):
        stamp = "2026-05-20T22:02:00+08:00"
        self.assertEqual(align_model_time_slot(stamp).minute, 0)
        with self.assertRaises(ValueError):
            align_model_time_slot(stamp, 119)
        self.assertEqual(align_model_time_slot("2026-05-20T22:02:01+08:00", 121).minute, 0)
        self.rows[-1]["observed_at"] = "2026-05-20T12:30:29+08:00"
        result = build_features([self.rows[-1]], self.rows[:-1], ["Alpha"], alignment_tolerance_seconds=20)
        self.assertIn("off_schedule_snapshot", [d["code"] for d in result["diagnostics"]])

    def test_duplicate_aligned_slot_blocks(self):
        other = dict(self.rows[-1], observed_at="2026-05-20T12:29:55+08:00")
        result = build_features([self.rows[-1]], self.rows[:-1]+[other], ["Alpha"])
        self.assertEqual(result["status"], "blocked")
        self.assertIn("duplicate_model_time_slot", [d["code"] for d in result["diagnostics"]])

    def test_round_up_uses_slot_for_calendar_and_cycles(self):
        rows = sequence(end="2026-05-17T23:59:30+08:00")
        result = self.build(rows)
        self.assertEqual(result["status"], "ready")
        row = result["features"].iloc[0]
        self.assertEqual([row[k] for k in ["day", "hour", "minute", "day_of_week", "time_slot", "week_slot"]], [18, 0, 0, 0, 0, 0])
        self.assertEqual(row.hour_cos, 1)
        self.assertEqual(result["stations"][0]["observed_at"], rows[-1]["observed_at"])

    def test_calendar_file_injection_and_coverage(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"calendar.json"
            # Synthetic test calendar, not an asserted official holiday.
            config = dict(coverage_start="2026-05-01", coverage_end="2026-05-31", holidays=["2026-05-20"])
            path.write_text(json.dumps(config), encoding="utf-8")
            result = build_features([self.rows[-1]], self.rows[:-1], ["Alpha"], holiday_calendar_path=path)
            self.assertEqual(result["features"].iloc[0].is_national_holiday, 1)
            self.assertEqual(result["features"].iloc[0].is_day_off, 1)
            config.update(coverage_end="2026-05-19", holidays=[])
            path.write_text(json.dumps(config), encoding="utf-8")
            result = build_features([self.rows[-1]], self.rows[:-1], ["Alpha"], holiday_calendar_path=path)
            self.assertEqual(result["status"], "blocked")
            self.assertIn("holiday_calendar_out_of_range", [d["code"] for d in result["diagnostics"]])

    def test_zero_capacity_ratio_nan(self):
        self.rows[-1].update(total_slots=0, available_bikes=0)
        self.assertTrue(math.isnan(kernel(self.rows).iloc[-1].bike_ratio))
        self.assertEqual(self.build()["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
