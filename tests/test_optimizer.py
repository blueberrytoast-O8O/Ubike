"""Synthetic tiny route fixtures only; no real model or large history loading."""
import copy
import json
import unittest
from pathlib import Path

from backend.optimizer import optimize, safety_quantity

NOW = "2026-09-12T22:30:29+08:00"


def fixture():
    stations = [dict(official_station_id=sid, station_name=sid, total_slots=total,
        available_bikes=bikes, available_docks=total-bikes, latitude=25., longitude=lon,
        observed_at=NOW) for sid, total, bikes, lon in
        [("A", 100, 84, 121.), ("B", 30, 1, 121.001), ("C", 20, 0, 121.002)]]
    alerts = [dict(alert_id=s["official_station_id"], official_station_id=s["official_station_id"],
        station_name=s["station_name"], alert_type="low_docks" if i == 0 else "low_bikes",
        severity="critical", status="active", observed_at=NOW, duration_minutes=30,
        triggered_by=["realtime"]) for i, s in enumerate(stations)]
    vehicles = [dict(vehicle_id="V1", latitude=25., longitude=121., current_load=0)]
    return alerts, stations, vehicles


class OptimizerTests(unittest.TestCase):
    def setUp(self):
        self.alerts, self.stations, self.vehicles = fixture()
        self.rules = json.loads((Path(__file__).parents[1]/"backend/config/optimizer_rules.json").read_text())

    def run_plan(self, **kwargs):
        return optimize(self.alerts, self.stations, self.vehicles, now=NOW, rules=self.rules, **kwargs)

    def steps(self):
        return self.run_plan()["candidate_plans"][0]["route_steps"]

    def test_core_A_B_C_no_return(self):
        steps = self.steps()
        self.assertEqual([(s["official_station_id"], s["quantity"]) for s in steps], [("A",14),("B",8),("C",6)])
        self.assertEqual([s["load_after"] for s in steps], [14,6,0])

    def test_load_conservation(self):
        for s in self.steps():
            self.assertEqual(s["load_after"], s["load_before"] + (s["quantity"] if s["action"] == "pickup" else -s["quantity"]))

    def test_existing_six_delivered_first(self):
        self.vehicles[0]["current_load"] = 6
        self.stations[2]["longitude"] = 121.0001
        self.stations.append(dict(self.stations[1], official_station_id="D", station_name="D",
                                 total_slots=40, available_bikes=0, available_docks=40, longitude=121.003))
        self.alerts.append(dict(self.alerts[1], alert_id="D", official_station_id="D", station_name="D"))
        steps = self.steps()
        self.assertEqual((steps[0]["action"],steps[0]["official_station_id"],steps[0]["quantity"]), ("dropoff","C",6))

    def test_capacity_14(self):
        self.assertTrue(all(0 <= s["load_after"] <= 14 for s in self.steps()))

    def test_capacity_25(self):
        self.vehicles[0]["capacity"] = 25
        self.stations[0]["available_bikes"] = 100
        self.stations[0]["available_docks"] = 0
        self.stations[1].update(total_slots=100, available_bikes=0, available_docks=100)
        plan = self.run_plan()["candidate_plans"][0]
        self.assertEqual(plan["vehicle_capacity"],25)
        self.assertEqual(plan["route_steps"][0]["quantity"],25)

    def test_destination_dock_limit(self):
        self.stations[1]["available_docks"] = 2
        steps = self.steps()
        self.assertEqual(sum(s["quantity"] for s in steps if s["official_station_id"] == "B"), 2)
        self.assertEqual(next(d["quantity"] for d in self.run_plan()["unmet_demand"] if d["alert_id"] == "B"), 6)

    def test_source_available_supply(self):
        self.stations[0]["available_bikes"] = 73
        self.assertEqual(self.steps()[0]["quantity"],3)

    def test_source_safety(self):
        picked = sum(s["quantity"] for s in self.steps() if s["action"] == "pickup")
        self.assertGreaterEqual(84-picked,70)

    def test_destination_needs_not_exceeded(self):
        for sid, need in [("B",8),("C",6)]:
            self.assertLessEqual(sum(s["quantity"] for s in self.steps() if s["official_station_id"]==sid),need)

    def test_locked_alert(self):
        result = self.run_plan(locked={"alert_ids":["B"]})
        self.assertTrue(any(e["alert_id"] == "B" and "locked" in e["reason_codes"] for e in result["excluded_alerts"]))
        self.assertFalse(any(s["alert_id"] == "B" for p in result["candidate_plans"] for s in p["route_steps"]))

    def test_published_task_station_lock(self):
        result = self.run_plan(locked={"tasks":[{"status":"published","sourceStationId":"A","targetStationId":"B"}]})
        self.assertEqual(result["candidate_plans"],[])

    def test_time_over_120_excluded(self):
        result = self.run_plan(road_time_provider=lambda a,b: dict(distance_km=1,duration_minutes=121))
        self.assertEqual(result["candidate_plans"],[])
        self.assertTrue(result["excluded_alerts"])

    def test_missing_coordinates(self):
        self.stations[0]["latitude"] = None
        result = self.run_plan()
        self.assertEqual(result["candidate_plans"],[])
        self.assertIn("missing_or_invalid_coordinates",[d["code"] for d in result["diagnostics"]])

    def test_haversine_label(self):
        self.assertEqual(self.run_plan()["candidate_plans"][0]["distance_method"],"estimated_haversine")

    def test_far_large_demand_not_preferred(self):
        self.vehicles[0]["current_load"] = 14
        far = dict(self.stations[1],official_station_id="D",station_name="D",total_slots=1000,available_bikes=0,available_docks=1000,longitude=122.)
        self.stations.append(far)
        self.alerts.append(dict(self.alerts[1],alert_id="D",official_station_id="D",station_name="D"))
        self.assertNotEqual(self.steps()[0]["official_station_id"],"D")

    def test_candidate_limit(self):
        self.vehicles = [dict(self.vehicles[0],vehicle_id=f"V{i:02d}") for i in range(15)]
        self.assertEqual(self.run_plan()["optimization_summary"]["evaluated_candidates"],10)
        self.rules["max_candidate_plans"] = 2
        self.assertEqual(self.run_plan()["optimization_summary"]["evaluated_candidates"],2)

    def test_score_recomputable(self):
        plan = self.run_plan()["candidate_plans"][0]
        score = plan["score_breakdown"]
        self.assertAlmostEqual(score["total"],sum(v for k,v in score.items() if k != "total"))
        self.assertAlmostEqual(score["total"],sum(s["score_breakdown"]["total"] for s in plan["route_steps"]))

    def test_reason_codes(self):
        plan = self.run_plan()["candidate_plans"][0]
        self.assertIn("cost_adjusted_priority",plan["reason_codes"])
        self.assertTrue(all(s["reason_codes"] for s in plan["route_steps"]))

    def test_prediction_blocked_supported(self):
        self.assertEqual(len(self.run_plan(prediction_status="blocked")["candidate_plans"]),1)

    def test_bad_station_isolated(self):
        self.stations.append(dict(self.stations[0],official_station_id="bad",total_slots=None))
        self.assertTrue(self.run_plan()["candidate_plans"])
        self.assertIn("invalid_station",[d["code"] for d in self.run_plan()["diagnostics"]])

    def test_confirmation_always_required(self):
        result = self.run_plan()
        self.assertIs(result["requires_admin_confirmation"],True)
        self.assertTrue(all(p["requires_admin_confirmation"] is True for p in result["candidate_plans"]))

    def test_strict_json(self):
        result = self.run_plan()
        self.assertEqual(json.loads(json.dumps(result,allow_nan=False)),result)

    def test_determinism(self):
        self.assertEqual(self.run_plan(),self.run_plan())

    def test_immutable_input(self):
        before = copy.deepcopy((self.alerts,self.stations,self.vehicles,self.rules))
        self.run_plan()
        self.assertEqual(before,(self.alerts,self.stations,self.vehicles,self.rules))

    def test_rounding_boundaries(self):
        self.assertEqual(safety_quantity(21,.3),7)
        self.assertEqual(safety_quantity(21,.7),15)
        self.assertEqual(safety_quantity(100,.7),70)

    def test_road_provider_used(self):
        result = self.run_plan(road_time_provider=lambda a,b:dict(distance_km=.1,duration_minutes=1))
        plan = result["candidate_plans"][0]
        self.assertEqual(plan["distance_method"],"road_provider")
        self.assertAlmostEqual(plan["estimated_total_distance_km"],.3)
        self.assertLessEqual(plan["estimated_total_minutes"],120)

    def test_no_vehicles_no_fabrication(self):
        self.vehicles=[]
        result=self.run_plan()
        self.assertEqual(result["candidate_plans"],[])
        self.assertEqual(result["optimization_summary"]["unmet_bikes"],14)

    def test_multiple_vehicles_do_not_double_allocate(self):
        self.vehicles.append(dict(self.vehicles[0],vehicle_id="V2"))
        result=self.run_plan()
        self.assertEqual(sum(s["quantity"] for p in result["candidate_plans"] for s in p["route_steps"] if s["action"]=="pickup"),14)

    def test_priority_severity_duration_and_forecast_sources(self):
        self.vehicles[0]["current_load"] = 6
        self.stations[1].update(total_slots=20, available_bikes=0, available_docks=20)
        self.stations[2]["longitude"] = self.stations[1]["longitude"]
        self.alerts[1].update(severity="warning",duration_minutes=0)
        self.alerts[2].update(duration_minutes=120,triggered_by=["realtime","forecast_30m","forecast_60m"])
        plan = self.run_plan(prediction_status="ready")["candidate_plans"][0]
        self.assertEqual(plan["route_steps"][0]["official_station_id"],"C")
        expected=40+120*.1+10+6+3
        self.assertEqual(plan["route_steps"][0]["score_breakdown"]["priority"],expected)

    def test_failed_road_provider_not_zero_distance(self):
        def failed(a,b):
            raise RuntimeError("road data unavailable")
        result=self.run_plan(road_time_provider=failed)
        self.assertEqual(result["candidate_plans"],[])
        self.assertIn("road_time_unavailable",[d["code"] for d in result["diagnostics"]])

    def test_completed_task_releases_station_lock(self):
        result=self.run_plan(locked={"tasks":[{"status":"completed","sourceStationId":"A"}]})
        self.assertEqual(len(result["candidate_plans"]),1)


if __name__ == "__main__":
    unittest.main()
