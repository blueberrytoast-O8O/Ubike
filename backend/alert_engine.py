"""Stateless alert evaluation; no models, tasks, persistence or external services.

evaluate_alerts(latest, predictions=None, prediction_status='unavailable',
                previous_state=None, rules=None, now=...) accepts normalizer and
model-service result dictionaries or their station/prediction lists. Rules may
be a JSON path or mapping. Feed the previous result back as previous_state.

Only ready/ok/partial prediction status permits real forecasts; every forecast
must match the station name, ID, capacity, exact observed_at and horizon time.
Realtime reported docks are authoritative. Forecast docks = total - predicted
bikes, explicitly diagnosed. Threshold crossings merge by official ID and type.
Recovery requires every usable source >= recovery threshold. No forecast is
invented when unavailable. Missing/invalid current stations carry prior alerts
as unverified (degraded); absence is not evidence of recovery.
"""
import copy
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")
DEFAULT_RULES = Path(__file__).with_name("config") / "alert_rules.json"


def _time(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result.astimezone(TAIPEI)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("finite numeric value required")
    return value


def _count(value):
    value = _number(value)
    if value < 0 or value != int(value):
        raise ValueError("nonnegative integer required")
    return int(value)


def _rows(value, key):
    if value is None:
        return []
    return value.get(key, []) if isinstance(value, dict) else value


def load_rules(rules=None):
    if isinstance(rules, dict):
        result = copy.deepcopy(rules)
    else:
        with Path(rules or DEFAULT_RULES).open(encoding="utf-8-sig") as handle:
            result = json.load(handle)
    for kind in ("low_bikes", "low_docks"):
        rule = result[kind]
        critical, trigger, recover = [_number(rule[k]) for k in
            ("critical_at_or_below_ratio", "trigger_below_ratio", "resolve_at_or_above_ratio")]
        if not 0 <= critical < trigger < recover <= 1:
            raise ValueError("Require 0 <= critical < trigger < recovery <= 1")
    return result


def evaluate_alerts(latest, predictions=None, prediction_status="unavailable",
                    previous_state=None, rules=None, now=None):
    """Return strict-JSON result; configuration errors degrade without raising."""
    diagnostics, active, resolved = [], [], []

    def note(code, sid=None, message=""):
        item = dict(code=code, official_station_id=sid if isinstance(sid, str) else None, message=str(message))
        diagnostics.append(item)
        return item

    def finish():
        return dict(status="degraded" if diagnostics or prediction_status not in ("ready", "ok") else "ok",
                    active_alerts=active, resolved_alerts=resolved, diagnostics=diagnostics,
                    prediction_status=prediction_status,
                    alert_summary=dict(active_count=len(active), resolved_count=len(resolved),
                        low_bikes=sum(a["alert_type"] == "low_bikes" for a in active),
                        low_docks=sum(a["alert_type"] == "low_docks" for a in active),
                        critical=sum(a["severity"] == "critical" for a in active),
                        realtime_alerts=sum("realtime" in a["triggered_by"] for a in active),
                        forecast_alerts=sum(bool(a["forecast_horizons"]) for a in active),
                        unverified_alerts=sum(a["status"] == "unverified" for a in active)))

    try:
        clock = _time(now)
        config = load_rules(rules)
        if not isinstance(prediction_status, str):
            raise ValueError("prediction_status must be a string")
    except (ValueError, TypeError, AttributeError, KeyError, OSError) as exc:
        prediction_status = prediction_status if isinstance(prediction_status, str) else "unavailable"
        note("invalid_alert_configuration", message=exc)
        return finish()

    previous = {}
    for prior in _rows(previous_state, "active_alerts"):
        try:
            item = copy.deepcopy(prior)
            json.dumps(item, allow_nan=False)
            key = (item["official_station_id"], item["alert_type"])
            if not isinstance(key[0], str) or key[1] not in ("low_bikes", "low_docks"):
                raise ValueError("invalid prior identity")
            if item["severity"] not in ("warning", "critical") or item["status"] not in ("active", "unverified"):
                raise ValueError("invalid prior state")
            if not _time(item["first_seen_at"]) <= _time(item["last_seen_at"]) <= clock:
                raise ValueError("prior time is invalid or execution time went backwards")
            for field in ("alert_id", "observed_at", "station_name", "triggered_by", "forecast_horizons", "reason_codes", "diagnostics"):
                item[field]
            if key in previous:
                raise ValueError("duplicate prior alert")
            previous[key] = item
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            note("invalid_previous_alert", message=exc)

    stations, duplicates = {}, set()
    for station in _rows(latest, "stations"):
        sid = station.get("official_station_id") if isinstance(station, dict) else None
        try:
            if not isinstance(sid, str) or not sid or not isinstance(station["station_name"], str) or not station["station_name"]:
                raise ValueError("station ID and name required")
            if sid in stations or sid in duplicates:
                duplicates.add(sid)
                stations.pop(sid, None)
                raise ValueError("duplicate current station")
            total = _count(station["total_slots"])
            if total == 0:
                raise ValueError("total_slots must be positive")
            bikes, docks = _count(station["available_bikes"]), _count(station["available_docks"])
            if bikes > total or docks > total:
                raise ValueError("count exceeds capacity")
            observed = _time(station["observed_at"])
            if observed > clock:
                raise ValueError("observation is in the future")
            stations[sid] = dict(station_name=station["station_name"], total=total, bikes=bikes, docks=docks, observed=observed)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            note("invalid_station", sid, exc)

    forecasts, duplicate_forecasts = {}, set()
    permitted = prediction_status in ("ready", "ok", "partial")
    if isinstance(predictions, dict) and predictions.get("status") not in (None, "ready", "ok", "partial"):
        permitted = False
    for forecast in _rows(predictions, "predictions") if permitted else []:
        sid = forecast.get("official_station_id") if isinstance(forecast, dict) else None
        try:
            station = stations[sid]
            horizon = forecast["horizon_minutes"]
            if type(horizon) is not int or horizon not in (30, 60) or forecast.get("model_mode") != "real":
                raise ValueError("requires real 30/60 minute forecast")
            if forecast["station_name"] != station["station_name"] or _count(forecast["total_slots"]) != station["total"]:
                raise ValueError("forecast station metadata mismatch")
            if _time(forecast["observed_at"]) != station["observed"]:
                raise ValueError("forecast belongs to another snapshot")
            target = _time(forecast["predicted_at"])
            if target != station["observed"] + timedelta(minutes=horizon) or target <= clock:
                raise ValueError("forecast horizon invalid or expired")
            bikes = _count(forecast["predicted_bikes"])
            if bikes > station["total"]:
                raise ValueError("forecast exceeds capacity")
            key = (sid, horizon)
            if key in forecasts or key in duplicate_forecasts:
                forecasts.pop(key, None)
                duplicate_forecasts.add(key)
                raise ValueError("duplicate forecast horizon")
            forecasts[key] = bikes
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            note("invalid_forecast", sid, exc)

    for sid, station in stations.items():
        for kind, count in (("low_bikes", station["bikes"]), ("low_docks", station["docks"])):
            key, rule = (sid, kind), config[kind]
            prior = previous.pop(key, None)
            sources = [("realtime", count / station["total"], None)]
            for horizon in (30, 60):
                if (sid, horizon) in forecasts:
                    bikes = forecasts[(sid, horizon)]
                    value = bikes if kind == "low_bikes" else station["total"] - bikes
                    sources.append((f"forecast_{horizon}m", value / station["total"], horizon))
            threshold = rule["resolve_at_or_above_ratio"] if prior else rule["trigger_below_ratio"]
            triggers = [s for s in sources if s[1] < threshold]
            if not triggers and prior is None:
                continue
            first = _time(prior["first_seen_at"]) if prior else clock
            alert = dict(alert_id=prior["alert_id"] if prior else str(uuid5(NAMESPACE_URL, f"{sid}:{kind}:{clock.isoformat()}")),
                official_station_id=sid, station_name=station["station_name"], alert_type=kind,
                severity="critical" if any(s[1] <= rule["critical_at_or_below_ratio"] for s in triggers) else "warning",
                status="active", observed_at=station["observed"].isoformat(), first_seen_at=first.isoformat(),
                last_seen_at=clock.isoformat(), duration_minutes=(clock-first).total_seconds()/60,
                current_bikes=station["bikes"], current_docks=station["docks"], total_slots=station["total"],
                triggered_by=[s[0] for s in triggers], forecast_horizons=[s[2] for s in triggers if s[2]],
                reason_codes=["below_trigger_ratio"] if any(s[1] < rule["trigger_below_ratio"] for s in triggers) else ["hysteresis_hold"],
                diagnostics=[])
            if not triggers:
                alert.update(status="resolved", severity=prior["severity"], resolved_at=clock.isoformat(),
                             reason_codes=["recovered_to_safe_range"])
                resolved.append(alert)
            else:
                if kind == "low_docks" and alert["forecast_horizons"]:
                    alert["diagnostics"].append(dict(code="forecast_docks_derived", message="total_slots - predicted_bikes; unavailable/disabled docks are unknown"))
                active.append(alert)

    for (sid, kind), prior in previous.items():
        # Do not claim recovery or a new observation when current data is absent.
        prior["status"] = "unverified"
        prior["diagnostics"].append(note("current_station_unavailable", sid, "Prior alert retained; recovery cannot be verified"))
        active.append(prior)
    return finish()
