"""Deterministic candidate-only dispatch heuristic; no publication or persistence.

optimize(active_alerts, stations, vehicles, locked=None, rules=None, now=None,
         road_time_provider=None, prediction_status='unavailable',
         alert_rules_path=None)

vehicles: [{vehicle_id, latitude, longitude, current_load, capacity?}].
Optional available=False excludes a vehicle. No default vehicle or coordinates.
locked: {alert_ids: [], station_ids: [], tasks: []}. Unfinished tasks lock all
station_ids, alert_ids, route_steps and legacy sourceStationId/targetStationId.
Only completed/cancelled tasks release locks; missing status remains locked.
Road provider(origin_latlon, destination_latlon) returns distance_km and
duration_minutes. Invalid provider results exclude that edge, no silent fallback.
Provider must be deterministic for deterministic output. No API is called here.

Plans reserve supply/demand jointly, so candidates do not double-allocate stock.
Routes serve feasible destinations with existing load before refilling. Empty
vehicles choose a pickup only if a subsequent dropoff fits the remaining time.
Every move has positive net score and is within current capacity/stock limits.
Model forecasts influence priority only; absent forecast inventory quantities
cannot justify invented pre-positioning demand. Unmet demand includes excluded
destinations and dock-limited demand. A partial route may retain vehicle load.
"""
import copy
import json
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from uuid import uuid5, NAMESPACE_URL

ROOT = Path(__file__).parent
TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")


def _read(value, default):
    if isinstance(value, dict):
        return copy.deepcopy(value)
    with Path(value or default).open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("finite number required")
    return value


def _int(value, minimum=0):
    value = _num(value)
    if value < minimum or int(value) != value:
        raise ValueError("invalid integer")
    return int(value)


def _point(row):
    lat, lon = _num(row.get("latitude")), _num(row.get("longitude"))
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("coordinate out of range")
    return (lat, lon)


def _time(value):
    t = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError("timezone required")
    return t.astimezone(TAIPEI)


def safety_quantity(total, ratio):
    """Central rounding: ceil(total * decimal ratio), no float-boundary loss."""
    return int((Decimal(total) * Decimal(str(ratio))).to_integral_value(rounding=ROUND_CEILING))


def _haversine(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 6371.0088 * 2 * math.asin(math.sqrt(min(1, max(0, h))))


def optimize(active_alerts, stations, vehicles, locked=None, rules=None, now=None,
             road_time_provider=None, prediction_status="unavailable", alert_rules_path=None):
    diagnostics, excluded, plans, demand = [], [], [], {}
    evaluated = 0

    def note(code, sid=None, message=""):
        diagnostics.append(dict(code=code, official_station_id=sid, message=str(message)))

    def result():
        unmet = [dict(alert_id=a, official_station_id=d["sid"], quantity=d["remaining"])
                 for a, d in sorted(demand.items()) if d["remaining"] > 0]
        return dict(candidate_plans=plans, excluded_alerts=excluded, unmet_demand=unmet,
                    diagnostics=diagnostics, requires_admin_confirmation=True,
                    optimization_summary=dict(candidate_count=len(plans), evaluated_candidates=evaluated,
                        excluded_alert_count=len(excluded), unmet_bikes=sum(d["quantity"] for d in unmet),
                        prediction_status=prediction_status, method="deterministic_greedy"))

    try:
        clock = _time(now)
        cfg = _read(rules, ROOT / "config/optimizer_rules.json")
        ar = _read(alert_rules_path, ROOT / "config/alert_rules.json")
        low = Decimal(str(ar["low_bikes"]["trigger_below_ratio"]))
        high = Decimal(1) - Decimal(str(ar["low_docks"]["trigger_below_ratio"]))
        if not 0 < low < high < 1:
            raise ValueError("invalid safety ratios")
        for k in ("default_vehicle_capacity", "max_candidate_plans", "max_route_steps"):
            _int(cfg[k], 1)
        for k in ("max_task_minutes", "estimated_speed_kmh", "duration_priority_cap_minutes"):
            if _num(cfg[k]) <= 0:
                raise ValueError("positive configuration required")
        for k in ("service_minutes_per_stop", "service_minutes_per_bike"):
            if _num(cfg[k]) < 0:
                raise ValueError("negative service time")
        weights = cfg["weights"]
        for k in ("critical", "warning", "duration_minute", "realtime", "forecast_30m", "forecast_60m", "quantity", "load_fit", "distance_km", "travel_minute"):
            if _num(weights[k]) < 0:
                raise ValueError("negative score weight")
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
        note("invalid_optimizer_configuration", message=exc)
        return result()

    locks = locked or {}
    locked_alerts, locked_stations = set(locks.get("alert_ids", [])), set(locks.get("station_ids", []))
    for task in locks.get("tasks", []):
        if task.get("status") in ("completed", "cancelled"):
            continue
        locked_alerts.update(task.get("alert_ids", task.get("addressed_alert_ids", [])))
        locked_stations.update(task.get("station_ids", []))
        locked_stations.update(task.get(k) for k in ("sourceStationId", "targetStationId") if task.get(k))
        for step in task.get("route_steps", []):
            locked_stations.add(step.get("official_station_id"))
            locked_alerts.add(step.get("alert_id"))

    raw_stations = stations.get("stations", []) if isinstance(stations, dict) else stations
    data, bad_ids = {}, set()
    for s in raw_stations:
        sid = s.get("official_station_id") if isinstance(s, dict) else None
        try:
            if not isinstance(sid, str) or not sid or not isinstance(s["station_name"], str):
                raise ValueError("station identity missing")
            if sid in data or sid in bad_ids:
                data.pop(sid, None)
                bad_ids.add(sid)
                raise ValueError("duplicate station")
            total, bikes, docks = _int(s["total_slots"], 1), _int(s["available_bikes"]), _int(s["available_docks"])
            if bikes > total or docks > total or _time(s["observed_at"]) > clock:
                raise ValueError("invalid station counts or future timestamp")
            data[sid] = dict(s, total_slots=total, available_bikes=bikes, available_docks=docks)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            note("invalid_station", sid if isinstance(sid, str) else None, exc)

    alerts = active_alerts.get("active_alerts", []) if isinstance(active_alerts, dict) else active_alerts
    nodes, seen_alerts, seen_keys = {}, set(), set()
    for alert in sorted(alerts, key=lambda a: str(a.get("alert_id", ""))):
        aid, sid = alert.get("alert_id"), alert.get("official_station_id")
        reason = None
        try:
            s = data[sid]
            kind = alert["alert_type"]
            if not isinstance(aid, str) or not aid or kind not in ("low_bikes", "low_docks"):
                raise ValueError("invalid alert identity")
            if aid in seen_alerts or (sid, kind) in seen_keys:
                raise ValueError("duplicate alert")
            seen_alerts.add(aid); seen_keys.add((sid, kind))
            if alert.get("status") != "active" or alert["station_name"] != s["station_name"] or _time(alert["observed_at"]) != _time(s["observed_at"]):
                raise ValueError("inactive or mismatched alert snapshot")
            sources = set(alert.get("triggered_by", []))
            if prediction_status in ("blocked", "warming_up", "unavailable"):
                sources &= {"realtime"}
            if not sources:
                reason = "no_usable_alert_source"
            total, bikes = s["total_slots"], s["available_bikes"]
            amount = max(0, safety_quantity(total, low)-bikes) if kind == "low_bikes" else max(0, bikes-safety_quantity(total, high))
            if kind == "low_bikes":
                demand[aid] = dict(sid=sid, remaining=amount)
            if sid in locked_stations or aid in locked_alerts:
                reason = "locked"
            elif amount == 0:
                reason = "no_current_safe_quantity"
            point = None
            try:
                point = _point(s)
            except (ValueError, TypeError) as exc:
                note("missing_or_invalid_coordinates", sid, exc)
                reason = reason or "missing_coordinates"
            if reason:
                excluded.append(dict(alert_id=aid, official_station_id=sid, reason_codes=[reason]))
                continue
            duration = _num(alert.get("duration_minutes", 0))
            if duration < 0 or alert["severity"] not in ("critical", "warning"):
                raise ValueError("invalid severity or duration")
            nodes[aid] = dict(alert=alert, station=s, point=point, remaining=min(amount, s["available_docks"]) if kind == "low_bikes" else amount,
                priority=weights[alert["severity"]] + min(duration, cfg["duration_priority_cap_minutes"])*weights["duration_minute"] + sum(weights[k] for k in sorted(sources) if k in ("realtime", "forecast_30m", "forecast_60m")))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            excluded.append(dict(alert_id=aid, official_station_id=sid, reason_codes=["invalid_alert_or_station"]))
            note("invalid_alert_or_station", sid, exc)

    edge_cache = {}
    def edge(a, b):
        key = (a, b)
        if key not in edge_cache:
            try:
                if road_time_provider is None:
                    distance = _haversine(a, b)
                    minutes = distance / cfg["estimated_speed_kmh"] * 60
                else:
                    value = road_time_provider(a, b)
                    distance, minutes = _num(value["distance_km"]), _num(value["duration_minutes"])
                if distance < 0 or minutes < 0:
                    raise ValueError("negative road distance/time")
                edge_cache[key] = (distance, minutes)
            except Exception as exc:
                note("road_time_unavailable", message=exc)
                edge_cache[key] = None
        return edge_cache[key]

    used_vehicles = set()
    for vehicle in sorted(vehicles or [], key=lambda v: str(v.get("vehicle_id", ""))):
        if evaluated >= cfg["max_candidate_plans"]:
            break
        if vehicle.get("available") is False:
            continue
        try:
            vid = vehicle["vehicle_id"]
            if not isinstance(vid, str) or not vid or vid in used_vehicles:
                raise ValueError("invalid/duplicate vehicle ID")
            used_vehicles.add(vid)
            capacity = _int(vehicle.get("capacity", cfg["default_vehicle_capacity"]), 1)
            load = _int(vehicle["current_load"])
            if load > capacity:
                raise ValueError("vehicle load exceeds capacity")
            point = _point(vehicle)
        except (ValueError, KeyError, TypeError) as exc:
            note("invalid_vehicle", message=exc)
            continue
        evaluated += 1
        initial, elapsed, distance_sum, steps, parts = load, 0.0, 0.0, [], []
        for _ in range(cfg["max_route_steps"]):
            options = []
            # Use current load before considering any refill.
            kind = "low_bikes" if load else "low_docks"
            for aid, node in sorted(nodes.items()):
                if node["alert"]["alert_type"] != kind or node["remaining"] <= 0:
                    continue
                qty = min(load if load else capacity, node["remaining"])
                if not load:
                    qty = min(qty, sum(n["remaining"] for n in nodes.values() if n["alert"]["alert_type"] == "low_bikes"))
                if qty <= 0:
                    continue
                trip = edge(point, node["point"])
                if trip is None:
                    continue
                dist, travel = trip
                service = cfg["service_minutes_per_stop"] + qty*cfg["service_minutes_per_bike"]
                if elapsed+travel+service > cfg["max_task_minutes"]:
                    continue
                if not load:
                    # Never collect for a destination that cannot be served in time.
                    feasible = False
                    for dest in nodes.values():
                        if dest["alert"]["alert_type"] != "low_bikes" or dest["remaining"] <= 0:
                            continue
                        onward = edge(node["point"], dest["point"])
                        delivery = min(qty, dest["remaining"])
                        if onward and elapsed+travel+service+onward[1]+cfg["service_minutes_per_stop"]+delivery*cfg["service_minutes_per_bike"] <= cfg["max_task_minutes"]:
                            feasible = True
                            break
                    if not feasible:
                        continue
                fit = min(load, node["remaining"])/max(load, node["remaining"]) if load else 0
                breakdown = dict(priority=node["priority"], quantity=qty*weights["quantity"],
                    load_fit=fit*weights["load_fit"], distance_penalty=-dist*weights["distance_km"],
                    time_penalty=-travel*weights["travel_minute"])
                score = sum(breakdown.values())
                if score > 0:
                    options.append((score, aid, qty, dist, travel, service, breakdown))
            if not options:
                break
            score, aid, qty, dist, travel, service, breakdown = sorted(options, key=lambda o: (-o[0], o[1]))[0]
            node = nodes[aid]
            before = load
            action = "dropoff" if load else "pickup"
            load += qty if action == "pickup" else -qty
            node["remaining"] -= qty
            if action == "dropoff":
                demand[aid]["remaining"] -= qty
            steps.append(dict(sequence=len(steps)+1, action=action, official_station_id=node["alert"]["official_station_id"],
                station_name=node["station"]["station_name"], alert_id=aid, quantity=qty, load_before=before, load_after=load,
                distance_from_previous_km=dist, estimated_arrival_at=(clock+timedelta(minutes=elapsed+travel)).isoformat(),
                estimated_service_minutes=service, reason_codes=["use_existing_load", "respect_destination_need_and_docks"] if action == "dropoff" else ["collect_only_above_source_safety_level"],
                score_breakdown=dict(breakdown, total=score)))
            parts.append(breakdown)
            elapsed += travel+service
            distance_sum += dist
            point = node["point"]
        if steps:
            totals = {key: sum(p[key] for p in parts) for key in parts[0]}
            signature = json.dumps([vid, clock.isoformat(), steps], sort_keys=True)
            plans.append(dict(plan_id=str(uuid5(NAMESPACE_URL, signature)), vehicle_id=vid, vehicle_capacity=capacity,
                initial_load=initial, route_steps=steps, estimated_total_distance_km=distance_sum,
                estimated_total_minutes=elapsed, distance_method="road_provider" if road_time_provider else "estimated_haversine",
                addressed_alert_ids=sorted({s["alert_id"] for s in steps}), remaining_load=load,
                reason_codes=["existing_load_before_refill", "cost_adjusted_priority", "hard_task_time_limit", "current_inventory_constraints"],
                score_breakdown=dict(totals, total=sum(totals.values())), requires_admin_confirmation=True))
    if not used_vehicles:
        note("no_usable_vehicle", message="Provide vehicle ID, current load and valid coordinates")
    addressed = {a for p in plans for a in p["addressed_alert_ids"]}
    for aid, node in sorted(nodes.items()):
        if aid not in addressed:
            excluded.append(dict(alert_id=aid, official_station_id=node["alert"]["official_station_id"],
                                 reason_codes=["no_feasible_route_within_time_cost_and_resources"]))
    return result()
