import copy
import json
import unittest
from datetime import datetime, timedelta

from backend.alert_engine import evaluate_alerts, load_rules

NOW = "2026-09-12T22:30:29+08:00"


def station(bikes=50, docks=50, total=100, sid="001"):
    return dict(official_station_id=sid, station_name="Station " + sid,
                observed_at=NOW, available_bikes=bikes, available_docks=docks, total_slots=total)


def forecast(bikes, horizon=30):
    return dict(official_station_id="001", station_name="Station 001", observed_at=NOW,
                predicted_at=(datetime.fromisoformat(NOW)+timedelta(minutes=horizon)).isoformat(),
                predicted_bikes=bikes, total_slots=100, horizon_minutes=horizon, model_mode="real")


class AlertEngineTests(unittest.TestCase):
    def run_alerts(self, rows=None, forecasts=None, status="ready", previous=None, now=NOW, rules=None):
        return evaluate_alerts(rows if rows is not None else [station()], forecasts,
                              status, previous, rules, now)

    def test_realtime_low_bikes(self):
        result = self.run_alerts([station(5, 95)])
        self.assertEqual(result["active_alerts"][0]["alert_type"], "low_bikes")

    def test_realtime_low_docks_uses_reported_value(self):
        result = self.run_alerts([station(50, 0)])
        self.assertEqual(result["active_alerts"][0]["alert_type"], "low_docks")
        self.assertEqual(result["active_alerts"][0]["current_docks"], 0)

    def test_safe(self):
        self.assertEqual(self.run_alerts()["active_alerts"], [])

    def test_station_size_uses_ratio(self):
        result = self.run_alerts([station(5, 5, 10), station(5, 95, 100, "002")])
        self.assertEqual([a["official_station_id"] for a in result["active_alerts"]], ["002"])

    def test_forecast_30(self):
        alert = self.run_alerts(forecasts=[forecast(5)])["active_alerts"][0]
        self.assertEqual(alert["triggered_by"], ["forecast_30m"])

    def test_forecast_60(self):
        alert = self.run_alerts(forecasts=[forecast(95, 60)])["active_alerts"][0]
        self.assertEqual(alert["forecast_horizons"], [60])
        self.assertEqual(alert["diagnostics"][0]["code"], "forecast_docks_derived")

    def test_merge_sources(self):
        result = self.run_alerts([station(5, 95)], [forecast(15), forecast(20, 60)])
        self.assertEqual(len(result["active_alerts"]), 1)
        alert = result["active_alerts"][0]
        self.assertEqual(alert["triggered_by"], ["realtime", "forecast_30m", "forecast_60m"])
        self.assertEqual(alert["forecast_horizons"], [30, 60])

    def test_realtime_severity_not_lowered(self):
        alert = self.run_alerts([station(0, 100)], [forecast(25)])["active_alerts"][0]
        self.assertEqual(alert["severity"], "critical")

    def test_warming_up_still_alerts(self):
        result = self.run_alerts([station(0, 100)], status="warming_up")
        self.assertEqual(result["prediction_status"], "warming_up")
        self.assertIn(result["status"], ["ok", "degraded"])
        self.assertEqual(len(result["active_alerts"]), 1)

    def test_blocked_ignores_forecasts(self):
        for status in ("blocked", "warming_up", "unavailable"):
            self.assertEqual(self.run_alerts(forecasts=[forecast(0)], status=status)["active_alerts"], [])

    def test_bad_station_isolated(self):
        result = self.run_alerts([station(total=None), station(0, 100, sid="002")])
        self.assertEqual(len(result["active_alerts"]), 1)
        self.assertEqual(result["diagnostics"][0]["code"], "invalid_station")

    def test_invalid_capacities(self):
        for value in (0, -1, None):
            result = self.run_alerts([station(total=value)])
            self.assertEqual(result["active_alerts"], [])
            self.assertTrue(result["diagnostics"])

    def test_continuation_duration(self):
        previous = self.run_alerts([station(5, 95)])
        later = "2026-09-12T23:00:29+08:00"
        result = self.run_alerts([station(5, 95)], previous=previous, now=later)
        alert = result["active_alerts"][0]
        self.assertEqual(alert["first_seen_at"], NOW)
        self.assertEqual(alert["duration_minutes"], 30)
        self.assertEqual(alert["alert_id"], previous["active_alerts"][0]["alert_id"])

    def test_recovery_resolves(self):
        previous = self.run_alerts([station(5, 95)])
        result = self.run_alerts(previous=previous)
        self.assertEqual(result["active_alerts"], [])
        self.assertEqual(result["resolved_alerts"][0]["status"], "resolved")

    def test_hysteresis(self):
        previous = self.run_alerts([station(29, 71)])
        held = self.run_alerts([station(31, 69)], previous=previous)
        self.assertEqual(len(held["active_alerts"]), 1)
        self.assertEqual(held["active_alerts"][0]["reason_codes"], ["hysteresis_hold"])
        result = self.run_alerts([station(35, 65)], previous=held)
        self.assertEqual(len(result["resolved_alerts"]), 1)
        self.assertEqual(self.run_alerts([station(31, 69)], previous=result)["active_alerts"], [])

    def test_no_input_mutation(self):
        rows, forecasts, rules = [station(5, 95)], [forecast(5)], load_rules()
        previous = self.run_alerts(rows)
        before = copy.deepcopy((rows, forecasts, previous, rules))
        self.run_alerts(rows, forecasts, previous=previous, rules=rules)
        self.assertEqual((rows, forecasts, previous, rules), before)

    def test_strict_json_and_required_fields(self):
        result = self.run_alerts([station(0, 100)], status="warming_up")
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)
        self.assertTrue(set("alert_id official_station_id station_name alert_type severity status observed_at first_seen_at last_seen_at duration_minutes current_bikes current_docks total_slots triggered_by forecast_horizons reason_codes diagnostics".split()) <= result["active_alerts"][0].keys())

    def test_config_injection(self):
        rules = load_rules()
        self.assertEqual(rules["provenance"], "team_configurable_default")
        rules["low_bikes"]["trigger_below_ratio"] = .2
        self.assertEqual(self.run_alerts([station(25, 75)], rules=rules)["active_alerts"], [])

    def test_invalid_forecast_never_replaces_realtime(self):
        for change in ({"model_mode": "mock"}, {"observed_at": "2026-09-12T22:00:09+08:00"}, {"predicted_bikes": float("nan")}, {"total_slots": 200}):
            pred = dict(forecast(0), **change)
            result = self.run_alerts(forecasts=[pred])
            self.assertEqual(result["active_alerts"], [])
            self.assertEqual(result["diagnostics"][0]["code"], "invalid_forecast")

    def test_missing_station_does_not_resolve(self):
        prior = self.run_alerts([station(0, 100)])
        result = self.run_alerts([], previous=prior)
        self.assertEqual(result["resolved_alerts"], [])
        self.assertEqual(result["active_alerts"][0]["status"], "unverified")

    def test_duplicate_forecasts_not_arbitrarily_selected(self):
        result = self.run_alerts(forecasts=[forecast(0), forecast(50)])
        self.assertEqual(result["active_alerts"], [])
        self.assertTrue(result["diagnostics"])

    def test_utc_is_converted_to_taipei(self):
        result = self.run_alerts([station(0, 100)], now="2026-09-12T14:30:29Z")
        self.assertEqual(result["active_alerts"][0]["first_seen_at"], NOW)


if __name__ == "__main__":
    unittest.main()
